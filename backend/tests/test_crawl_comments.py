"""评论产物的读取、归一化、建树，以及补抓形态规则的测试。

这些是纯函数与文件读取，不碰数据库、不起子进程 —— 评论这块最容易出错的地方
恰恰是「各平台字段名/哨兵值不统一」，逐条钉住比端到端测更省事也更准。

字段映射的依据是 MC 各 store/{platform}/__init__.py 的落盘字典，与
crawl_comments.COMMENT_FIELD_MAP 的注释一一对应。
"""

import json
from pathlib import Path

from app.services import crawl_comments
from tests.fakes import mc_comment


def _write_comments(output_dir: Path, platform: str, rows: list, *, name: str = "search") -> Path:
    """往输出目录写一份评论 jsonl（目录约定与 MC 一致：{平台}/jsonl/）。"""
    target = output_dir / platform / "jsonl"
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{name}_comments_fake.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _ids(nodes: list) -> list:
    """取一层节点的 id 列表（断言顺序用）。"""
    return [node["id"] for node in nodes]


# ---------------------------------------------------------------------------
# 文件定位
# ---------------------------------------------------------------------------


class TestCommentsFiles:
    """评论 jsonl 的定位（与 contents_files 同一套约定，但只认 comments）。"""

    def test_only_comments_files_are_picked(self, tmp_path):
        """contents 文件不在结果里 —— 两个模块各读各的，边界不能糊。"""
        _write_comments(tmp_path, "xhs", [mc_comment("c1")])
        contents = tmp_path / "xhs" / "jsonl" / "search_contents_fake.jsonl"
        contents.write_text("{}\n", encoding="utf-8")

        files = crawl_comments.comments_files(tmp_path, "xhs")
        assert [path.name for path in files] == ["search_comments_fake.jsonl"]

    def test_missing_dir_is_empty(self, tmp_path):
        """开关关掉 / max_comments=0 时评论文件根本不存在（不是空文件）。"""
        assert crawl_comments.comments_files(tmp_path, "xhs") == []


# ---------------------------------------------------------------------------
# 逐平台字段映射
# ---------------------------------------------------------------------------


class TestNormalizeComment:
    """各平台评论行 → 统一形态。字段名差异是这块最大的坑。"""

    def test_xhs_fields_and_pictures(self):
        row = mc_comment(
            "c1",
            like_count=7,
            create_time=1747000000000,
            parent_comment_id="c0",
            sub_comment_count=2,
            pictures="https://img/1.webp,https://img/2.webp",
        )
        node = crawl_comments.normalize_comment(row, "xhs")

        assert node["id"] == "c1"
        assert node["content"] == "评论-c1"
        assert node["nickname"] == "路人甲"
        assert node["liked_count"] == "7"
        assert node["sub_comment_count"] == 2
        assert node["pictures"] == ["https://img/1.webp", "https://img/2.webp"]
        assert node["parent_id"] == "c0"
        assert node["orphan"] is False and node["children"] == []
        # 毫秒时间戳按量级归一成 ISO（复用 crawl_results 的口径）
        assert node["created_at"] == "2025-05-11T21:46:40Z"

    def test_weibo_like_field_name_differs(self):
        """微博的点赞叫 comment_like_count（主内容那边才叫 liked_count）。"""
        row = mc_comment(
            "c1",
            comment_like_count=11,
            parent_comment_id="c0",
            sub_comment_count=1,
        )
        row.pop("like_count")

        node = crawl_comments.normalize_comment(row, "wb")
        assert node["liked_count"] == "11"
        assert node["parent_id"] == "c0"

    def test_tieba_uses_user_nickname_and_string_time(self):
        """贴吧：昵称是 user_nickname、时间是日期字符串、没有点赞字段。"""
        row = mc_comment(
            "c1",
            user_nickname="贴吧用户",
            publish_time="2024-03-05 12:00:00",
            parent_comment_id="c0",
        )
        row.pop("nickname")
        row.pop("like_count")

        node = crawl_comments.normalize_comment(row, "tieba")
        assert node["nickname"] == "贴吧用户"
        assert node["created_at"] == "2024-03-05 12:00:00"
        # 平台不落盘点赞：给空串，前端要能区分「0 个赞」和「这平台没有赞」
        assert node["liked_count"] == ""

    def test_zhihu_uses_user_nickname_and_second_timestamp(self):
        row = mc_comment(
            "c1",
            user_nickname="知乎用户",
            publish_time=1747000000,  # 秒
            like_count=9,
        )
        row.pop("nickname")

        node = crawl_comments.normalize_comment(row, "zhihu")
        assert node["nickname"] == "知乎用户"
        assert node["liked_count"] == "9"
        assert node["created_at"] == "2025-05-11T21:46:40Z"

    def test_kuaishou_has_neither_like_nor_parent(self):
        """快手两个字段都不落盘：层级永久丢失（只能全平铺），点赞给空串。"""
        row = mc_comment("c1")
        row.pop("like_count")
        row.pop("parent_comment_id")

        node = crawl_comments.normalize_comment(row, "ks")
        assert node["liked_count"] == ""
        assert node["parent_id"] == ""

    def test_none_id_is_cleaned(self):
        """快手可能落 None；微博缺字段时落的是字面字符串 "None" —— 都不算 id。"""
        assert crawl_comments.normalize_comment(mc_comment(None), "xhs")["id"] == ""
        assert crawl_comments.normalize_comment(mc_comment("None"), "xhs")["id"] == ""


# ---------------------------------------------------------------------------
# 建树
# ---------------------------------------------------------------------------


class TestGroupComments:
    """平铺的评论行 → 树。三个必须做对的地方见 group_comments 的 docstring。"""

    @staticmethod
    def _node(node_id: str, parent: str = "", created: str = "") -> dict:
        """造一个已经归一化过的节点（跳过 jsonl 那一层，直接测建树）。"""
        return {
            "id": node_id,
            "content": f"评论-{node_id}",
            "nickname": "路人甲",
            "liked_count": "0",
            "created_at": created,
            "sub_comment_count": 0,
            "pictures": [],
            "parent_id": parent,
            "orphan": False,
            "children": [],
        }

    def test_parent_child_nesting(self):
        rows = [
            self._node("c1"),
            self._node("c2", parent="c1"),
            self._node("c3", parent="c2"),
        ]
        roots = crawl_comments.group_comments(rows)

        assert _ids(roots) == ["c1"]
        assert _ids(roots[0]["children"]) == ["c2"]
        assert _ids(roots[0]["children"][0]["children"]) == ["c3"]

    def test_top_level_sentinels_differ_per_platform(self):
        """顶层父值各平台不统一（"0" / "" / "None"）—— 用真值判断会吞掉 "0"。"""
        for sentinel in ("", "0", "None", "none", None):
            rows = [self._node("c1", parent=sentinel), self._node("c2", parent=sentinel)]
            roots = crawl_comments.group_comments(rows)

            assert _ids(roots) == ["c1", "c2"], f"哨兵值 {sentinel!r} 没被认成顶层"
            assert roots[0]["orphan"] is False

    def test_orphan_is_promoted_and_flagged(self):
        """父没抓到（截断/风控）时子评论提升为一级，但必须带 orphan 标。"""
        rows = [self._node("c1"), self._node("c2", parent="missing")]

        roots = crawl_comments.group_comments(rows)
        assert _ids(roots) == ["c1", "c2"]
        by_id = {node["id"]: node for node in roots}
        assert by_id["c2"]["orphan"] is True
        assert by_id["c1"]["orphan"] is False

    def test_cycle_is_treated_as_orphan(self):
        """父链成环时不能无限转：两个节点都提为一级并打孤儿标。"""
        rows = [self._node("c1", parent="c2"), self._node("c2", parent="c1")]
        roots = crawl_comments.group_comments(rows)

        assert sorted(_ids(roots)) == ["c1", "c2"]
        assert all(node["orphan"] for node in roots)

    def test_depth_is_capped(self):
        """超深的节点提升到顶层但**不打**孤儿标 —— 它的父确实抓到了。"""
        rows = [
            self._node("c1"),
            self._node("c2", parent="c1"),
            self._node("c3", parent="c2"),
            self._node("c4", parent="c3"),  # 第 4 层，超过 MAX_DEPTH=3
        ]
        roots = crawl_comments.group_comments(rows)
        by_id = {node["id"]: node for node in roots}

        assert _ids(roots) == ["c1", "c4"]
        assert by_id["c4"]["orphan"] is False
        assert _ids(by_id["c1"]["children"]) == ["c2"]

    def test_children_sorted_by_time_desc(self):
        """同一层内新的在前；时间认不出来的沉底。"""
        rows = [
            self._node("old", created="2024-01-01T00:00:00Z"),
            self._node("new", created="2025-01-01T00:00:00Z"),
            self._node("unknown"),
            self._node("mid", created="2024-06-01T00:00:00Z"),
        ]
        roots = crawl_comments.group_comments(rows)
        assert _ids(roots) == ["new", "mid", "old", "unknown"]

    def test_parent_id_is_not_leaked(self):
        """parent_id 是建树的中间产物，不该出现在对外结果里。"""
        rows = [self._node("c1"), self._node("c2", parent="c1")]
        roots = crawl_comments.group_comments(rows)

        assert "parent_id" not in roots[0]
        assert "parent_id" not in roots[0]["children"][0]

    def test_empty_rows(self):
        assert crawl_comments.group_comments([]) == []

    def test_count_comments_includes_all_levels(self):
        rows = [
            self._node("c1"),
            self._node("c2", parent="c1"),
            self._node("c3", parent="c2"),
            self._node("c4"),
        ]
        roots = crawl_comments.group_comments(rows)
        assert crawl_comments.count_comments(roots) == 4
        assert len(roots) == 2


# ---------------------------------------------------------------------------
# 读取：去重键、笔记过滤、坏行
# ---------------------------------------------------------------------------


class TestScanComments:
    """从 jsonl 里读出这条笔记的评论（去重语义与 contents 一致：保留后写的）。"""

    def test_filters_other_notes(self, tmp_path):
        """一份 comments 文件里可能有多个笔记的评论，只收当前这条。"""
        _write_comments(
            tmp_path,
            "xhs",
            [mc_comment("c1"), mc_comment("c2", note_id="other")],
        )
        rows = crawl_comments.scan_comments(tmp_path, "xhs", "note1")
        assert [row["id"] for row in rows] == ["c1"]

    def test_duplicate_keeps_the_later_row(self, tmp_path):
        """MC 是 append 语义：同一批评论重抓时后写的字段更全，必须保留后写的。"""
        _write_comments(
            tmp_path,
            "xhs",
            [
                mc_comment("c1", sub_comment_count=0),
                mc_comment("c1", sub_comment_count=5),
            ],
        )
        rows = crawl_comments.scan_comments(tmp_path, "xhs", "note1")
        assert len(rows) == 1
        assert rows[0]["sub_comment_count"] == 5

    def test_missing_id_falls_back_to_content_fingerprint(self, tmp_path):
        """快手可能没有 comment_id：不能拿空串当键，否则所有无 id 评论并成一条。"""
        # 快手的笔记关联字段是 video_id（与主内容的 FIELD_MAP 同一张表）
        rows = [
            mc_comment(None, content="第一条", create_time=1747000000000),
            mc_comment(None, content="第二条", create_time=1747000001000),
        ]
        for row in rows:
            row["video_id"] = row.pop("note_id")
        _write_comments(tmp_path, "ks", rows)

        found = crawl_comments.scan_comments(tmp_path, "ks", "note1")
        assert [row["content"] for row in found] == ["第一条", "第二条"]
        # 同一条重写（内容与时间都一样）才该被合并
        _write_comments(tmp_path, "ks", rows, name="detail")
        assert len(crawl_comments.scan_comments(tmp_path, "ks", "note1")) == 2

    def test_literal_none_string_is_not_an_id(self, tmp_path):
        """微博缺字段时落的是字符串 "None"，两条不同的评论不能被它并成一条。"""
        rows = [
            mc_comment("None", content="A"),
            mc_comment("None", content="B"),
        ]
        _write_comments(tmp_path, "wb", rows)

        found = crawl_comments.scan_comments(tmp_path, "wb", "note1")
        assert sorted(row["content"] for row in found) == ["A", "B"]

    def test_bad_lines_are_skipped(self, tmp_path):
        """半行 JSON（进程被杀时常见）跳过，不影响其余行。"""
        path = _write_comments(tmp_path, "xhs", [mc_comment("c1")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"comment_id": "c2"\n')  # 半行
            handle.write("\n")                      # 空行
            handle.write("[1, 2]\n")                # 不是对象

        rows = crawl_comments.scan_comments(tmp_path, "xhs", "note1")
        assert [row["id"] for row in rows] == ["c1"]

    def test_missing_file_returns_empty(self, tmp_path):
        """没开评论采集时文件不存在 —— 这是正常态，不是错误。"""
        assert crawl_comments.scan_comments(tmp_path, "xhs", "note1") == []

    def test_unknown_platform_returns_empty(self, tmp_path):
        assert crawl_comments.scan_comments(tmp_path, "taobao", "note1") == []


class TestCollectComments:
    """多个输出目录合并成一棵树（原任务 + 各次补抓）。"""

    def test_merges_directories_and_later_wins(self, tmp_path):
        root_dir = tmp_path / "job_1"
        refetch_dir = tmp_path / "job_2"
        _write_comments(root_dir, "xhs", [mc_comment("c1", sub_comment_count=0)])
        _write_comments(
            refetch_dir,
            "xhs",
            [
                mc_comment("c1", sub_comment_count=4),  # 同一批评论重抓，后写的胜
                mc_comment("c2"),
            ],
        )

        roots = crawl_comments.collect_comments([root_dir, refetch_dir], "xhs", "note1")
        assert _ids(roots) == ["c1", "c2"]
        assert roots[0]["sub_comment_count"] == 4

    def test_builds_tree_across_directories(self, tmp_path):
        """父在原任务目录、子只在补抓目录 —— 合并后才拼得成树。"""
        root_dir = tmp_path / "job_1"
        refetch_dir = tmp_path / "job_2"
        _write_comments(root_dir, "xhs", [mc_comment("c1")])
        _write_comments(
            refetch_dir, "xhs", [mc_comment("c2", parent_comment_id="c1")]
        )

        roots = crawl_comments.collect_comments([root_dir, refetch_dir], "xhs", "note1")
        assert _ids(roots) == ["c1"]
        assert _ids(roots[0]["children"]) == ["c2"]

    def test_missing_directories_are_ignored(self, tmp_path):
        roots = crawl_comments.collect_comments([tmp_path / "nope"], "xhs", "note1")
        assert roots == []


# ---------------------------------------------------------------------------
# 补抓形态：逐平台喂给 --specified_id 的值不一样
# ---------------------------------------------------------------------------


class TestRefetchSpec:
    """能不能补抓、喂什么形态。纯函数，逐平台可单测。"""

    XHS_URL = "https://www.xiaohongshu.com/explore/abc?xsec_token=TOKEN&xsec_source=pc_search"

    def test_xhs_uses_url_when_token_present(self):
        note = {"id": "abc", "url": self.XHS_URL}
        assert crawl_comments.refetch_spec("xhs", note) == self.XHS_URL

    def test_xhs_without_token_is_refused(self):
        note = {"id": "abc", "url": "https://www.xiaohongshu.com/explore/abc"}
        assert crawl_comments.refetch_spec("xhs", note) is None

    def test_xhs_with_literal_none_token_is_refused(self):
        """MC 是 f-string 插值：字段缺失时拼出 xsec_token=None，照样是个合法 URL。"""
        note = {
            "id": "abc",
            "url": "https://www.xiaohongshu.com/explore/abc?xsec_token=None&xsec_source=pc_search",
        }
        assert crawl_comments.refetch_spec("xhs", note) is None

    def test_dy_and_ks_use_url(self):
        for platform in ("dy", "ks", "tieba"):
            note = {"id": "abc", "url": f"https://example.com/{platform}/abc"}
            assert crawl_comments.refetch_spec(platform, note) == note["url"]

    def test_weibo_uses_bare_id_not_url(self):
        """微博的 detail 分支直接 get_note_info_task(note_id=...)，喂 URL 会坏。"""
        note = {"id": "5012345", "url": "https://m.weibo.cn/detail/5012345"}
        assert crawl_comments.refetch_spec("wb", note) == "5012345"

    def test_weibo_without_id_is_refused(self):
        assert crawl_comments.refetch_spec("wb", {"id": "None", "url": "x"}) is None

    def test_bilibili_is_always_refused(self):
        """产物里只有 av 号，MC 的解析正则只认 BV —— 补抓必然 0 条。"""
        note = {"id": "BV1xx", "url": "https://www.bilibili.com/video/av123456"}
        assert crawl_comments.refetch_spec("bili", note) is None

    def test_zhihu_page_url_ok_cdn_url_refused(self):
        assert (
            crawl_comments.refetch_spec(
                "zhihu", {"url": "https://www.zhihu.com/answer/123"}
            )
            == "https://www.zhihu.com/answer/123"
        )
        # zvideo 有时只存了 CDN 直链（_extract_zvideo_content 的另一个分支）
        assert (
            crawl_comments.refetch_spec(
                "zhihu", {"url": "https://vdn.example.com/video/abc.m3u8"}
            )
            is None
        )

    def test_missing_url_is_refused(self):
        assert crawl_comments.refetch_spec("dy", {"id": "a", "url": ""}) is None


class TestRefetchUnsupportedReason:
    """不能补抓时的文案（能补抓时给空串，前端据此决定按钮灰不灰）。"""

    def test_empty_when_supported(self):
        note = {"id": "abc", "url": TestRefetchSpec.XHS_URL}
        assert crawl_comments.refetch_unsupported_reason("xhs", note) == ""

    def test_bili_reason_names_the_cause(self):
        reason = crawl_comments.refetch_unsupported_reason("bili", {"url": "x"})
        assert "BV" in reason

    def test_xhs_reason_mentions_token(self):
        reason = crawl_comments.refetch_unsupported_reason(
            "xhs", {"url": "https://www.xiaohongshu.com/explore/a"}
        )
        assert "xsec_token" in reason

    def test_generic_reason_for_missing_url(self):
        reason = crawl_comments.refetch_unsupported_reason("dy", {"id": "a", "url": ""})
        assert reason


# ---------------------------------------------------------------------------
# 评论图片本地化：URL 懒下载缓存（打桩 _http_get_picture，不发真网络请求）
# ---------------------------------------------------------------------------


class TestLocalizePictures:
    """评论图是带时效签名的 URL，读的时候要就地缓存成本地文件。"""

    @staticmethod
    def _node(pictures: list) -> dict:
        return {"id": "c1", "pictures": pictures, "children": []}

    @staticmethod
    def _stub_fetch(monkeypatch, results: dict, calls: list = None):
        """把 _http_get_picture 换成查表桩；不在表里的 URL 抛网络异常。

        results: {url: (status, body, content_type)}；calls 收集实际被请求的 URL。
        """

        def fake_fetch(url):
            if calls is not None:
                calls.append(url)
            try:
                return results[url]
            except KeyError as exc:
                raise OSError("network down") from exc

        monkeypatch.setattr(crawl_comments, "_http_get_picture", fake_fetch)

    def test_success_replaces_url_with_local_path(self, tmp_path, monkeypatch):
        """下到了 → pictures 换成相对输出目录的路径，文件落在 comment_media/。"""
        self._stub_fetch(
            monkeypatch,
            {"https://img/1.webp": (200, b"webp-bytes", "image/webp")},
        )
        nodes = [self._node(["https://img/1.webp"])]

        crawl_comments.localize_pictures(tmp_path, "xhs", nodes)

        cached = nodes[0]["pictures"][0]
        assert cached.startswith(f"xhs/{crawl_comments.COMMENT_MEDIA_DIR}/")
        assert cached.endswith(".webp")
        assert (tmp_path / cached).read_bytes() == b"webp-bytes"

    def test_second_call_hits_cache_without_network(self, tmp_path, monkeypatch):
        """幂等：缓存命中后不再发请求（弹窗轮询期间每 5 秒读一次）。"""
        calls: list = []
        self._stub_fetch(
            monkeypatch, {"https://img/1.webp": (200, b"x", "image/webp")}, calls
        )
        nodes = [self._node(["https://img/1.webp"])]
        crawl_comments.localize_pictures(tmp_path, "xhs", nodes)
        first = nodes[0]["pictures"][0]

        nodes = [self._node(["https://img/1.webp"])]
        crawl_comments.localize_pictures(tmp_path, "xhs", nodes)

        assert nodes[0]["pictures"][0] == first
        assert calls == ["https://img/1.webp"]

    def test_client_error_writes_skip_marker(self, tmp_path, monkeypatch):
        """4xx（签名过期）救不回来：写 .skip，URL 原样透传且不再重试。"""
        calls: list = []
        self._stub_fetch(monkeypatch, {"https://img/gone": (403, b"", "")}, calls)
        for _ in range(2):
            nodes = [self._node(["https://img/gone"])]
            crawl_comments.localize_pictures(tmp_path, "xhs", nodes)
            assert nodes[0]["pictures"] == ["https://img/gone"]

        assert calls == ["https://img/gone"]  # 第二轮直接被标记挡住
        key = crawl_comments._picture_key("https://img/gone")
        assert (tmp_path / "xhs" / crawl_comments.COMMENT_MEDIA_DIR / f"{key}.skip").is_file()

    def test_network_error_keeps_url_without_marker(self, tmp_path, monkeypatch):
        """网络异常可能是本地抖动：保持 URL、不写 .skip，下一轮还会再试。"""
        calls: list = []
        self._stub_fetch(monkeypatch, {}, calls)

        for _ in range(2):
            nodes = [self._node(["https://img/flaky"])]
            crawl_comments.localize_pictures(tmp_path, "xhs", nodes)
            assert nodes[0]["pictures"] == ["https://img/flaky"]

        assert calls == ["https://img/flaky"] * 2

    def test_server_error_keeps_url_without_marker(self, tmp_path, monkeypatch):
        """5xx 同网络异常处理：这一轮放弃，但不判死刑。"""
        self._stub_fetch(monkeypatch, {"https://img/busy": (503, b"", "")})
        nodes = [self._node(["https://img/busy"])]

        crawl_comments.localize_pictures(tmp_path, "xhs", nodes)

        assert nodes[0]["pictures"] == ["https://img/busy"]
        key = crawl_comments._picture_key("https://img/busy")
        assert not (tmp_path / "xhs" / crawl_comments.COMMENT_MEDIA_DIR / f"{key}.skip").exists()

    def test_batch_cap_leaves_rest_as_url(self, tmp_path, monkeypatch):
        """单次最多下 PICTURE_MAX_BATCH 张：超出的保持 URL，本轮不判死刑。"""
        urls = [f"https://img/{i}.webp" for i in range(5)]
        self._stub_fetch(
            monkeypatch, {url: (200, b"x", "image/webp") for url in urls}
        )
        monkeypatch.setattr(crawl_comments, "PICTURE_MAX_BATCH", 2)

        nodes = [self._node(urls)]
        crawl_comments.localize_pictures(tmp_path, "xhs", nodes)

        localized = [p for p in nodes[0]["pictures"] if not p.startswith("http")]
        assert localized  # 前两张换成了本地路径
        assert len(localized) <= 2
        # 没下到的保持 URL 原样（下一轮轮询 / 翻页再补），也没有被标记
        assert len(nodes[0]["pictures"]) == len(urls)

    def test_skipped_urls_do_not_consume_the_batch_cap(self, tmp_path, monkeypatch):
        """已标记 4xx 的 URL 不占配额 —— 否则图多的笔记里新图永远轮不到。"""
        calls: list = []
        self._stub_fetch(monkeypatch, {"https://img/fresh.webp": (200, b"x", "image/webp")}, calls)
        monkeypatch.setattr(crawl_comments, "PICTURE_MAX_BATCH", 1)

        # 先让三张图拿到 403 标记
        for url in ("https://img/a", "https://img/b", "https://img/c"):
            monkeypatch.setattr(
                crawl_comments, "_http_get_picture", lambda u, _u=url: (403, b"", "")
            )
            crawl_comments.localize_pictures(tmp_path, "xhs", [self._node([url])])

        # 配额只有 1：如果被标记的 URL 还占额度，这张新图就下不到
        self._stub_fetch(monkeypatch, {"https://img/fresh.webp": (200, b"x", "image/webp")}, calls)
        nodes = [self._node(["https://img/a", "https://img/b", "https://img/c", "https://img/fresh.webp"])]
        crawl_comments.localize_pictures(tmp_path, "xhs", nodes)

        assert calls == ["https://img/fresh.webp"]
        assert not nodes[0]["pictures"][3].startswith("http")

    def test_children_pictures_are_localized_too(self, tmp_path, monkeypatch):
        """子评论里的图也要缓存 —— 树是递归走的。"""
        self._stub_fetch(
            monkeypatch, {"https://img/child.webp": (200, b"c", "image/webp")}
        )
        nodes = [self._node([])]
        nodes[0]["children"] = [self._node(["https://img/child.webp"])]

        crawl_comments.localize_pictures(tmp_path, "xhs", nodes)

        child_picture = nodes[0]["children"][0]["pictures"][0]
        assert child_picture.startswith(f"xhs/{crawl_comments.COMMENT_MEDIA_DIR}/")

    def test_unknown_content_type_falls_back_to_jpg(self, tmp_path, monkeypatch):
        self._stub_fetch(monkeypatch, {"https://img/odd": (200, b"x", "application/octet-stream")})
        nodes = [self._node(["https://img/odd"])]

        crawl_comments.localize_pictures(tmp_path, "xhs", nodes)

        assert nodes[0]["pictures"][0].endswith(".jpg")

    def test_non_http_values_pass_through(self, tmp_path, monkeypatch):
        """非 URL 的值（防御：理论上不出现）原样保留，不发请求。"""
        calls: list = []
        self._stub_fetch(monkeypatch, {}, calls)
        nodes = [self._node(["xhs/comment_media/abc.webp"])]

        crawl_comments.localize_pictures(tmp_path, "xhs", nodes)

        assert nodes[0]["pictures"] == ["xhs/comment_media/abc.webp"]
        assert calls == []

    def test_no_pictures_is_noop(self, tmp_path, monkeypatch):
        """没有图的评论（绝大多数平台）一个请求都不发。"""
        calls: list = []
        self._stub_fetch(monkeypatch, {}, calls)
        crawl_comments.localize_pictures(tmp_path, "tieba", [self._node([])])
        assert calls == []
