"""素材抓取产物层的测试（crawl_results.py）：归一化、去重、本地媒体、路径闸门。

全部用 tmp_path 里的假 jsonl 驱动，不依赖任何真实抓取产物。
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.models.crawl_job import CrawlPlatform
from app.services import crawl_results
from tests.fakes import mc_note


def _write_jsonl(output_dir: Path, platform: str, name: str, rows) -> Path:
    """按 MC 的落盘约定写一份 jsonl：{out}/{platform}/jsonl/{name}。"""
    target = output_dir / platform / "jsonl"
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


class TestScanNotes:
    """跨平台字段归一化（FIELD_MAP 的口径）。"""

    def test_xhs_normalization(self, tmp_path):
        """xhs：毫秒时间戳转 ISO、逗号图片串拆列表、cover 回退第一张图。"""
        _write_jsonl(
            tmp_path, "xhs", "search_contents_2026-09-19.jsonl", [mc_note("n1")]
        )
        notes = crawl_results.scan_notes(tmp_path, "xhs")

        assert len(notes) == 1
        note = notes[0]
        assert note["id"] == "n1"
        assert note["title"] == "标题-n1"
        assert note["nickname"] == "测试博主"
        assert note["liked_count"] == "10"
        assert note["collected_count"] == "5"
        assert note["url"] == "https://www.xiaohongshu.com/explore/n1"
        assert note["images"] == [
            "https://img.example/1.webp",
            "https://img.example/2.webp",
        ]
        # xhs 落盘没有 cover 字段，统一形态里用第一张图兜底
        assert note["cover"] == "https://img.example/1.webp"
        # time 是毫秒时间戳，归一化成 ISO（带 Z）
        assert note["publish_time"] == (
            datetime.fromtimestamp(1747000000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def test_collected_count_mapping(self, tmp_path):
        """收藏数：xhs/dy 落盘叫 collected_count，B 站叫 video_favorite_count，没有的平台给空串。"""
        _write_jsonl(
            tmp_path,
            "bili",
            "search_contents_2026-09-19.jsonl",
            [
                {
                    "video_id": "v1",
                    "title": "B站视频",
                    "desc": "",
                    "nickname": "up主",
                    "liked_count": 3,
                    "video_favorite_count": 8,
                    "video_comment": 1,
                    "video_share_count": 2,
                    "create_time": 1747000000,
                    "video_url": "https://www.bilibili.com/video/v1",
                    "source_keyword": "k",
                }
            ],
        )
        _write_jsonl(
            tmp_path,
            "ks",
            "search_contents_2026-09-19.jsonl",
            [
                {
                    "video_id": "k1",
                    "title": "快手视频",
                    "desc": "",
                    "nickname": "老铁",
                    "liked_count": 4,
                    "create_time": 1747000000,
                    "video_url": "https://www.kuaishou.com/short-video/k1",
                    "source_keyword": "k",
                }
            ],
        )

        bili_note = crawl_results.scan_notes(tmp_path, "bili")[0]
        ks_note = crawl_results.scan_notes(tmp_path, "ks")[0]
        assert bili_note["collected_count"] == "8"  # B 站的收藏叫 favorite
        assert ks_note["collected_count"] == ""  # 快手 jsonl 里没有收藏字段

    def test_dedupe_keeps_last_row(self, tmp_path):
        """搜索页先写一条、详情页再写一条是常态：同 id 保留后写的（字段更全）。

        文件按最后写入时间排序（字母序的 detail/search 与写入先后正好相反），
        这里用 utime 把先后关系钉死，别依赖写盘瞬间的时钟精度。
        """
        search = _write_jsonl(
            tmp_path,
            "xhs",
            "search_contents_2026-09-19.jsonl",
            [mc_note("n1", title="旧标题"), mc_note("n2")],
        )
        detail = _write_jsonl(
            tmp_path,
            "xhs",
            "detail_contents_2026-09-19.jsonl",
            [mc_note("n1", title="新标题", liked_count=999)],
        )
        os.utime(search, (1000, 1000))
        os.utime(detail, (2000, 2000))

        notes = crawl_results.scan_notes(tmp_path, "xhs")

        assert [note["id"] for note in notes] == ["n1", "n2"]  # 首次出现的顺序
        assert notes[0]["title"] == "新标题"
        assert notes[0]["liked_count"] == "999"

    def test_bad_lines_skipped(self, tmp_path):
        """空行 / 半行 JSON / 非对象行 / 缺 id 行都不进结果。"""
        target = tmp_path / "xhs" / "jsonl"
        target.mkdir(parents=True)
        (target / "search_contents_2026-09-19.jsonl").write_text(
            "\n"
            + json.dumps(mc_note("ok")) + "\n"
            + "{broken json\n"
            + json.dumps(["not", "a", "dict"]) + "\n"
            + json.dumps({"title": "没有 id"}) + "\n",
            encoding="utf-8",
        )
        notes = crawl_results.scan_notes(tmp_path, "xhs")
        assert [note["id"] for note in notes] == ["ok"]

    def test_weibo_title_falls_back_to_desc(self, tmp_path):
        """微博没有标题字段：标题用正文截断，也没有图片列表。"""
        _write_jsonl(
            tmp_path,
            "wb",
            "search_contents_2026-09-19.jsonl",
            [
                {
                    "note_id": "w1",
                    "content": "微博正文" + "长" * 100,
                    "liked_count": 3,
                    "comments_count": 1,
                    "shared_count": 2,
                    "create_time": 1747000000,  # 秒级时间戳
                    "note_url": "https://weibo.com/w1",
                    "nickname": "博主",
                    "source_keyword": "热搜",
                }
            ],
        )
        notes = crawl_results.scan_notes(tmp_path, "wb")

        assert len(notes) == 1
        note = notes[0]
        assert note["title"] == note["desc"][:60]
        assert note["desc"].startswith("微博正文")
        assert note["images"] == []
        assert note["share_count"] == "2"  # 微博的 share 叫 shared_count
        assert note["publish_time"].startswith("2025-")  # 秒级也能转 ISO

    def test_zhihu_prefers_content_text(self, tmp_path):
        """知乎正文在 content_text，desc 只是摘要；展示用正文。"""
        _write_jsonl(
            tmp_path,
            "zhihu",
            "search_contents_2026-09-19.jsonl",
            [
                {
                    "content_id": "z1",
                    "content_type": "answer",
                    "title": "如何选保温杯？",
                    "desc": "摘要",
                    "content_text": "完整正文",
                    "voteup_count": 42,
                    "comment_count": 5,
                    "created_time": 1747000000,
                    "content_url": "https://www.zhihu.com/answer/z1",
                    "user_nickname": "答主",
                    "source_keyword": "保温杯",
                }
            ],
        )
        notes = crawl_results.scan_notes(tmp_path, "zhihu")

        assert notes[0]["desc"] == "完整正文"
        assert notes[0]["liked_count"] == "42"  # 知乎的赞叫 voteup_count

    def test_tieba_string_date_passthrough(self, tmp_path):
        """贴吧的 publish_time 本来就是日期字符串，原样保留。"""
        _write_jsonl(
            tmp_path,
            "tieba",
            "search_contents_2026-09-19.jsonl",
            [
                {
                    "note_id": "t1",
                    "title": "吧帖",
                    "desc": "内容",
                    "publish_time": "2026-01-02 03:04:05",
                    "note_url": "https://tieba.baidu.com/p/t1",
                    "user_nickname": "吧友",
                    "total_replay_num": 7,
                }
            ],
        )
        notes = crawl_results.scan_notes(tmp_path, "tieba")
        assert notes[0]["publish_time"] == "2026-01-02 03:04:05"
        assert notes[0]["comment_count"] == "7"  # 贴吧的评论叫 total_replay_num

    def test_unknown_platform_returns_empty(self, tmp_path):
        assert crawl_results.scan_notes(tmp_path, "no-such-platform") == []


class TestCountNotes:
    """进度用的行数统计（与去重口径不同：见 count_notes 注释）。"""

    def test_counts_lines_across_files(self, tmp_path):
        _write_jsonl(
            tmp_path, "xhs", "search_contents_a.jsonl", [mc_note("n1"), mc_note("n2")]
        )
        _write_jsonl(tmp_path, "xhs", "detail_contents_a.jsonl", [mc_note("n3")])
        assert crawl_results.count_notes(tmp_path, "xhs") == 3

    def test_missing_directory_is_zero(self, tmp_path):
        assert crawl_results.count_notes(tmp_path, "xhs") == 0

    def test_comment_files_not_counted(self, tmp_path):
        """评论文件（*_comments_*）不是笔记，不得混进进度。"""
        _write_jsonl(
            tmp_path, "xhs", "search_contents_a.jsonl", [mc_note("n1")]
        )
        _write_jsonl(
            tmp_path,
            "xhs",
            "search_comments_a.jsonl",
            [{"comment_id": "c1"}, {"comment_id": "c2"}],
        )
        assert crawl_results.count_notes(tmp_path, "xhs") == 1


class TestLocalMedia:
    """本地媒体探测：平台短名→媒体目录长名的对照只在此处生效。"""

    def test_xhs_images_and_videos(self, tmp_path):
        folder = tmp_path / "xhs" / "images" / "n1"
        folder.mkdir(parents=True)
        (folder / "2.jpg").write_bytes(b"img2")
        (folder / "1.webp").write_bytes(b"img1")
        (folder / "note.txt").write_bytes(b"not media")
        video_dir = tmp_path / "xhs" / "videos" / "n1"
        video_dir.mkdir(parents=True)
        (video_dir / "v.mp4").write_bytes(b"video")

        images, videos = crawl_results.local_media(tmp_path, "xhs", "n1")
        # 排序保证顺序稳定（前端图片墙按这个顺序渲染）
        assert images == ["xhs/images/n1/1.webp", "xhs/images/n1/2.jpg"]
        assert videos == ["xhs/videos/n1/v.mp4"]

    def test_dy_uses_long_dir_name(self, tmp_path):
        """jsonl 目录用短名 dy，媒体目录是长名 douyin。"""
        folder = tmp_path / "douyin" / "images" / "a1"
        folder.mkdir(parents=True)
        (folder / "0.jpeg").write_bytes(b"img")

        images, videos = crawl_results.local_media(tmp_path, "dy", "a1")
        assert images == ["douyin/images/a1/0.jpeg"]
        assert videos == []

    def test_weibo_flat_layout_not_associated(self, tmp_path):
        """微博图片平铺无 note_id 目录：按笔记关联无意义，返回空。"""
        folder = tmp_path / "weibo" / "images"
        folder.mkdir(parents=True)
        (folder / "pic1.jpg").write_bytes(b"img")
        assert crawl_results.local_media(tmp_path, "wb", "w1") == ([], [])

    def test_collect_results_attaches_media(self, tmp_path):
        _write_jsonl(tmp_path, "xhs", "search_contents_a.jsonl", [mc_note("n1")])
        folder = tmp_path / "xhs" / "images" / "n1"
        folder.mkdir(parents=True)
        (folder / "1.webp").write_bytes(b"img")

        notes = crawl_results.collect_results(tmp_path, "xhs")
        assert notes[0]["local_images"] == ["xhs/images/n1/1.webp"]
        assert notes[0]["local_videos"] == []


class TestMediaFileGate:
    """media_file 是媒体接口的安全闸门：越界一律 None（404）。"""

    def test_inside_file_resolved(self, tmp_path):
        media = tmp_path / "xhs" / "images" / "n1" / "1.webp"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"img")
        assert crawl_results.media_file(tmp_path, "xhs/images/n1/1.webp") == media

    def test_parent_traversal_blocked(self, tmp_path):
        secret = tmp_path.parent / "secret.txt"
        secret.write_text("outside", encoding="utf-8")
        assert crawl_results.media_file(tmp_path, "../secret.txt") is None

    def test_absolute_path_blocked(self, tmp_path):
        """给绝对路径也不能逃出输出目录。"""
        other = tmp_path.parent / "elsewhere.jpg"
        other.write_bytes(b"x")
        assert crawl_results.media_file(tmp_path, str(other)) is None

    def test_missing_or_directory_returns_none(self, tmp_path):
        assert crawl_results.media_file(tmp_path, "xhs/images/n1/nope.webp") is None
        folder = tmp_path / "xhs" / "images" / "n1"
        folder.mkdir(parents=True)
        assert crawl_results.media_file(tmp_path, "xhs/images/n1") is None
