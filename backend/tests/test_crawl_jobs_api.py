"""素材抓取接口测试：信封格式、输入校验、状态机与产物读取。

执行层（子进程、结果判定）的测试在 test_crawl_runner.py，
产物归一化的测试在 test_crawl_results.py —— 这里只钉接口层的契约。
"""

import json
from pathlib import Path

import pytest

from app.core.config import settings
from tests.fakes import mc_comment, mc_note

#: 任务输出目录必须指到 tmp（conftest._isolate_crawl_output），否则假产物
#: 会写进开发机真实的 materials/crawl/。
pytestmark = pytest.mark.usefixtures("_isolate_crawl_output")


def _create_job(client, **extra) -> dict:
    """创建一个默认的 xhs 搜索任务并返回响应 data。"""
    payload = {
        "platform": "xhs",
        "crawler_type": "search",
        "login_type": "qrcode",
        "keywords": ["保温杯"],
        **extra,
    }
    response = client.post("/api/v1/crawl/jobs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


#: 能补抓的小红书链接：带 xsec_token（不带会被服务端 400 拦掉）。
XHS_URL_WITH_TOKEN = (
    "https://www.xiaohongshu.com/explore/n1?xsec_token=TOKEN&xsec_source=pc_search"
)


def _write_jsonl(output_dir: Path, platform: str, kind: str, rows: list) -> None:
    """往任务输出目录写假 jsonl（模拟 MC 已跑完落盘）。

    kind 是 contents / comments —— 两个文件同目录不同名，读取侧也是各读各的。
    """
    target = Path(output_dir) / platform / "jsonl"
    target.mkdir(parents=True, exist_ok=True)
    with (target / f"search_{kind}_fake.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _stub_env_payload(**overrides) -> dict:
    """一份「环境就绪」的探测结果（接口层测试不跑真探测）。"""
    payload = {
        "installed": True,
        "ready": True,
        "launcher": ["/fake/python"],
        "kind": "venv-python",
        "mc_root": "/fake/MediaCrawler",
        "python_version": "3.11.16",
        "detail": "",
        "node_version": "v20.11.0",
        "node_required_platforms": ["dy", "zhihu"],
        "login_states": [],
        "media_enabled": True,
        "zhihu_creator_cli_supported": True,
        "default_output_dir": "/tmp/materials/crawl",
        "install_hints": [],
        "warnings": [],
    }
    payload.update(overrides)
    return payload


class TestEnvironment:
    """环境自检接口。"""

    def test_environment_envelope(self, client, monkeypatch):
        """信封格式 + 安装指引与 Node 依赖平台都在 data 里。"""
        monkeypatch.setattr(
            "app.api.v1.crawl_jobs.probe_environment",
            lambda refresh=False: _stub_env_payload(
                installed=False,
                ready=False,
                install_hints=[
                    {"title": "克隆仓库", "command": "git clone <mc>", "note": "", "url": ""}
                ],
            ),
        )
        response = client.get("/api/v1/crawl/environment")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is True
        data = body["data"]
        assert data["installed"] is False
        assert data["install_hints"][0]["command"] == "git clone <mc>"
        assert "dy" in data["node_required_platforms"]

    def test_environment_refresh_passthrough(self, client, monkeypatch):
        """?refresh=1 必须传到探测层（「重新检测」按钮靠它绕过缓存）。"""
        seen = []

        def fake_probe(refresh=False):
            seen.append(refresh)
            return _stub_env_payload()

        monkeypatch.setattr("app.api.v1.crawl_jobs.probe_environment", fake_probe)
        client.get("/api/v1/crawl/environment")
        client.get("/api/v1/crawl/environment", params={"refresh": True})
        assert seen == [False, True]


class TestCreateJob:
    """创建任务的校验与信封。"""

    def test_create_search_job(self, client):
        data = _create_job(client)

        assert data["status"] == "pending"
        assert data["platform"] == "xhs"
        assert data["platform_label"] == "小红书"
        assert data["params"]["keywords"] == ["保温杯"]
        # 预估总量 = 每词上限(默认 20) × 关键词数
        assert data["expected_count"] == 20
        assert data["output_dir"].endswith(f"job_{data['id']}")
        assert data["progress_percent"] == 0

    def test_response_never_contains_cookies(self, client):
        """login_cookies 是凭据：请求模型收、数据库存，任何响应都不回显。"""
        data = _create_job(client, login_type="cookie", cookies="  a=1;b=2  ")

        assert "cookies" not in data
        assert "login_cookies" not in data
        assert "cookies" not in data["params"]

    def test_create_detail_job_expected_count(self, client):
        data = _create_job(
            client,
            crawler_type="detail",
            keywords=None,
            ids=["https://www.xiaohongshu.com/explore/a", "654321"],
        )
        assert data["expected_count"] == 2

    def test_qrcode_login_allows_headless(self, client):
        """扫码登录与无头不互斥：MC 的二维码走系统看图软件，不依赖浏览器窗口。"""
        data = _create_job(client, headless=True)
        assert data["params"]["headless"] is True

    def test_cookie_login_allows_headless(self, client):
        data = _create_job(
            client, login_type="cookie", cookies="a=1", headless=True
        )
        assert data["params"]["headless"] is True

    def test_max_notes_clamped(self, client):
        data = _create_job(client, max_notes=99999)
        assert data["params"]["max_notes"] == settings.CRAWL_MAX_NOTES_LIMIT

    def test_not_installed_returns_400(self, client, monkeypatch):
        """未装 MediaCrawler → 400 且失败要快，不排队。"""
        monkeypatch.setattr(
            "app.services.crawl_job_service.probe_environment",
            lambda refresh=False: _stub_env_payload(installed=False, ready=False),
        )
        response = client.post(
            "/api/v1/crawl/jobs",
            json={
                "platform": "xhs",
                "crawler_type": "search",
                "login_type": "qrcode",
                "keywords": ["保温杯"],
            },
        )
        assert response.status_code == 400, response.text
        assert "MediaCrawler" in response.json()["error"]["message"]

    def test_zhihu_creator_without_patch_returns_400(self, client, monkeypatch):
        """知乎 creator 模式依赖 MC 补丁：没打补丁就给指引，不排队后炸。"""
        monkeypatch.setattr(
            "app.services.crawl_job_service.probe_environment",
            lambda refresh=False: _stub_env_payload(zhihu_creator_cli_supported=False),
        )
        response = client.post(
            "/api/v1/crawl/jobs",
            json={
                "platform": "zhihu",
                "crawler_type": "creator",
                "login_type": "qrcode",
                "creators": ["https://www.zhihu.com/people/a"],
            },
        )
        assert response.status_code == 400, response.text
        assert "补丁" in response.json()["error"]["message"]

    def test_search_without_keywords_returns_400(self, client):
        response = client.post(
            "/api/v1/crawl/jobs",
            json={
                "platform": "xhs",
                "crawler_type": "search",
                "login_type": "qrcode",
                "keywords": [],
            },
        )
        assert response.status_code == 400, response.text

    def test_cookie_login_without_cookies_returns_400(self, client):
        response = client.post(
            "/api/v1/crawl/jobs",
            json={
                "platform": "xhs",
                "crawler_type": "search",
                "login_type": "cookie",
                "keywords": ["保温杯"],
                "cookies": "  ",
            },
        )
        assert response.status_code == 400, response.text

    def test_unknown_platform_returns_422(self, client):
        response = client.post(
            "/api/v1/crawl/jobs",
            json={
                "platform": "taobao",
                "crawler_type": "search",
                "login_type": "qrcode",
                "keywords": ["保温杯"],
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["success"] is False


class TestListAndDetail:
    """列表与详情。"""

    def test_list_and_detail(self, client):
        created = _create_job(client)

        response = client.get("/api/v1/crawl/jobs")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == created["id"]

        detail = client.get(f"/api/v1/crawl/jobs/{created['id']}")
        assert detail.status_code == 200
        assert detail.json()["data"]["id"] == created["id"]

    def test_get_missing_job_returns_404(self, client):
        response = client.get("/api/v1/crawl/jobs/9999")
        assert response.status_code == 404
        assert response.json()["success"] is False

    def test_status_filter(self, client):
        created = _create_job(client)
        client.post(f"/api/v1/crawl/jobs/{created['id']}/cancel")

        cancelled = client.get("/api/v1/crawl/jobs", params={"status": "cancelled"})
        assert cancelled.json()["data"]["total"] == 1
        pending = client.get("/api/v1/crawl/jobs", params={"status": "pending"})
        assert pending.json()["data"]["total"] == 0

    def test_platform_filter(self, client):
        _create_job(client)
        _create_job(client, platform="wb", keywords=["热搜"])

        only_wb = client.get("/api/v1/crawl/jobs", params={"platform": "wb"})
        assert only_wb.json()["data"]["total"] == 1
        assert only_wb.json()["data"]["items"][0]["platform"] == "wb"


class TestCancelAndDelete:
    """状态机的接口面。"""

    def test_cancel_then_delete(self, client):
        data = _create_job(client)

        response = client.post(f"/api/v1/crawl/jobs/{data['id']}/cancel")
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "cancelled"

        deleted = client.delete(f"/api/v1/crawl/jobs/{data['id']}")
        assert deleted.status_code == 200
        assert deleted.json()["data"] == {"id": data["id"]}
        assert client.get(f"/api/v1/crawl/jobs/{data['id']}").status_code == 404

    def test_cancel_terminal_job_returns_409(self, client):
        data = _create_job(client)
        client.post(f"/api/v1/crawl/jobs/{data['id']}/cancel")
        response = client.post(f"/api/v1/crawl/jobs/{data['id']}/cancel")
        assert response.status_code == 409

    def test_delete_unfinished_job_returns_409(self, client):
        """没结束的任务不许删（先取消），避免「记录没了进程还在跑」。"""
        data = _create_job(client)
        response = client.delete(f"/api/v1/crawl/jobs/{data['id']}")
        assert response.status_code == 409


class TestResultsAndLogAndMedia:
    """产物三件套：结果列表、日志尾部、本地媒体文件。"""

    def _produce_notes(self, output_dir: Path, count: int = 2) -> None:
        """往任务输出目录写假 jsonl（模拟 MC 已跑完落盘）。"""
        target = output_dir / "xhs" / "jsonl"
        target.mkdir(parents=True, exist_ok=True)
        with (target / "search_contents_fake.jsonl").open("w", encoding="utf-8") as handle:
            for index in range(1, count + 1):
                handle.write(json.dumps(mc_note(f"n{index}"), ensure_ascii=False) + "\n")

    def test_results_normalized(self, client):
        data = _create_job(client)
        self._produce_notes(Path(data["output_dir"]))

        response = client.get(f"/api/v1/crawl/jobs/{data['id']}/results")
        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["total"] == 2
        first = body["notes"][0]
        assert first["index"] == 1
        assert first["id"] == "n1"
        assert first["title"] == "标题-n1"
        assert len(first["images"]) == 2
        assert first["local_images"] == []  # 没下载媒体时是空列表而不是 null
        assert first["local_image_dir"] == ""  # 同理：没有图就没有目录，前端据此禁用换背景入口
        assert first["cover"] == first["images"][0]

    def test_results_with_local_media(self, client):
        data = _create_job(client)
        self._produce_notes(Path(data["output_dir"]), count=1)
        media = Path(data["output_dir"]) / "xhs" / "images" / "n1"
        media.mkdir(parents=True)
        (media / "1.webp").write_bytes(b"img-bytes")

        body = client.get(f"/api/v1/crawl/jobs/{data['id']}/results").json()["data"]
        assert body["notes"][0]["local_images"] == ["xhs/images/n1/1.webp"]
        # 绝对目录要能直接交给换背景当原图目录（本机路径，不是相对路径）
        assert Path(body["notes"][0]["local_image_dir"]) == media

        file_response = client.get(
            f"/api/v1/crawl/jobs/{data['id']}/media/xhs/images/n1/1.webp"
        )
        assert file_response.status_code == 200
        assert file_response.content == b"img-bytes"

    def test_media_traversal_returns_404(self, client):
        """越出任务输出目录的路径必须 404 —— 放开校验就是任意文件读取。

        %2E%2E 是 ".." 的 URL 编码：httpx 客户端会把裸 ".." 规范化掉，
        编码后才能让服务端真正收到一个穿越路径。
        """
        data = _create_job(client)
        secret = Path(data["output_dir"]).parent / "secret.txt"
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("outside", encoding="utf-8")

        response = client.get(
            f"/api/v1/crawl/jobs/{data['id']}/media/%2E%2E/secret.txt"
        )
        assert response.status_code == 404

    def test_media_missing_file_returns_404(self, client):
        data = _create_job(client)
        response = client.get(
            f"/api/v1/crawl/jobs/{data['id']}/media/xhs/images/n1/nope.webp"
        )
        assert response.status_code == 404

    def test_log_tail(self, client):
        data = _create_job(client)
        (Path(data["output_dir"])).mkdir(parents=True, exist_ok=True)
        (Path(data["output_dir"]) / "mc.log").write_text(
            "INFO 开始\nERROR 失败\n", encoding="utf-8"
        )

        response = client.get(f"/api/v1/crawl/jobs/{data['id']}/log")
        assert response.status_code == 200
        assert "ERROR 失败" in response.json()["data"]["log"]

    def test_log_of_missing_file_is_empty_string(self, client):
        """mc.log 还没生成（任务刚创建）也不该 500，给空串即可。"""
        data = _create_job(client)
        response = client.get(f"/api/v1/crawl/jobs/{data['id']}/log")
        assert response.status_code == 200
        assert response.json()["data"]["log"] == ""


class TestBatchDelete:
    """批量删除：整批成功或整批失败（POST /jobs/batch-delete）。"""

    def _create_terminal(self, client) -> int:
        created = _create_job(client)
        client.post(f"/api/v1/crawl/jobs/{created['id']}/cancel")
        return created["id"]

    def test_batch_delete_success(self, client):
        ids = [self._create_terminal(client) for _ in range(2)]

        response = client.post("/api/v1/crawl/jobs/batch-delete", json={"ids": ids})
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"ids": ids, "count": 2}
        for job_id in ids:
            assert client.get(f"/api/v1/crawl/jobs/{job_id}").status_code == 404

    def test_batch_delete_non_terminal_conflict_rolls_back(self, client):
        terminal_id = self._create_terminal(client)
        pending = _create_job(client)["id"]

        response = client.post(
            "/api/v1/crawl/jobs/batch-delete", json={"ids": [terminal_id, pending]}
        )
        assert response.status_code == 409, response.text
        assert client.get(f"/api/v1/crawl/jobs/{terminal_id}").status_code == 200

    def test_batch_delete_empty_ids_422(self, client):
        response = client.post("/api/v1/crawl/jobs/batch-delete", json={"ids": []})
        assert response.status_code == 422, response.text


class TestDeletePurge:
    """删除任务时可选连产物一起清（purge_files）—— 抓取的产物是整个输出目录。"""

    def _terminal_with_products(self, client) -> tuple[int, Path]:
        """造一条终态任务，并在输出目录里留下 jsonl 与一个媒体文件。"""
        created = _create_job(client)
        out_dir = Path(created["output_dir"])
        # 输出目录建任务时还不存在（跑起来才由 runner 建），这里手工造产物
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "notes.jsonl").write_text(
            json.dumps({"note_id": "n1", "title": "测试笔记"}, ensure_ascii=False),
            encoding="utf-8",
        )
        media = out_dir / "媒体" / "封面.jpg"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"fake-image")
        client.post(f"/api/v1/crawl/jobs/{created['id']}/cancel")
        return created["id"], out_dir

    def test_delete_without_purge_keeps_products(self, client):
        """默认只删记录：jsonl 与媒体文件原样保留。"""
        job_id, out_dir = self._terminal_with_products(client)

        response = client.delete(f"/api/v1/crawl/jobs/{job_id}")
        assert response.status_code == 200, response.text
        assert (out_dir / "notes.jsonl").is_file()
        assert (out_dir / "媒体" / "封面.jpg").is_file()

    def test_delete_with_purge_removes_products(self, client):
        """purge_files=true：整个输出目录（含子目录）都清掉。"""
        job_id, out_dir = self._terminal_with_products(client)

        response = client.delete(f"/api/v1/crawl/jobs/{job_id}?purge_files=true")
        assert response.status_code == 200, response.text
        assert not out_dir.exists(), f"输出目录没清掉：{out_dir}"


class TestRetry:
    """重试：按旧任务的参数**新建一条任务**（POST /jobs/{id}/retry）。

    抓取没有条目级状态（一条任务就是一个 MC 子进程），所以重试只有整任务重跑
    一种形态；而重跑必须是新建而不是就地重跑 —— 输出目录按任务 id 定死、
    MC 的 jsonl 又是 append 语义（就地重跑会把 note_count 算成两倍）。
    """

    def test_retry_creates_a_new_job(self, client):
        """返回 201 + 一条全新的 pending 任务，输出目录跟着新 id 走。"""
        created = _create_job(client)

        response = client.post(f"/api/v1/crawl/jobs/{created['id']}/retry")
        assert response.status_code == 201, response.text
        new = response.json()["data"]

        assert new["id"] != created["id"]
        assert new["status"] == "pending"
        assert new["output_dir"].endswith(f"job_{new['id']}")
        assert new["output_dir"] != created["output_dir"]
        # 预估口径与新建任务完全一致
        assert new["expected_count"] == created["expected_count"]
        assert new["params"] == created["params"]
        assert new["crawled_count"] == 0 and new["note_count"] == 0

    def test_old_job_and_its_products_are_untouched(self, client):
        """旧任务的记录与磁盘产物一个字都不许动 —— 这是「新建」的意义。"""
        created = _create_job(client)
        out_dir = Path(created["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "notes.jsonl").write_text("{}", encoding="utf-8")

        client.post(f"/api/v1/crawl/jobs/{created['id']}/retry")
        client.post(f"/api/v1/crawl/jobs/{created['id']}/cancel")

        detail = client.get(f"/api/v1/crawl/jobs/{created['id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["output_dir"] == created["output_dir"]
        assert (out_dir / "notes.jsonl").is_file()

    def test_cookie_login_is_rebuilt_server_side(self, client, db_session):
        """cookie 登录的任务也能重试，且 cookie 原样带过去。

        login_cookies 是登录凭据、任何响应都不回显，前端重建不出这种任务 ——
        所以这必须是服务端从旧任务行里读的（本用例直接查库核对）。
        """
        from app.models.crawl_job import CrawlJob

        created = _create_job(client, login_type="cookie", cookies=" a=1;b=2 ")

        response = client.post(f"/api/v1/crawl/jobs/{created['id']}/retry")
        assert response.status_code == 201, response.text
        new = response.json()["data"]

        assert new["login_type"] == "cookie"
        assert "cookies" not in new and "login_cookies" not in new
        assert db_session.get(CrawlJob, new["id"]).login_cookies == "a=1;b=2"

    def test_params_are_carried_over(self, client):
        """详情模式的 ids、翻页、条数上限……原样带进新任务。"""
        created = _create_job(
            client,
            crawler_type="detail",
            keywords=None,
            ids=["https://www.xiaohongshu.com/explore/a"],
            start_page=3,
            max_notes=7,
            get_comments=False,
        )

        new = client.post(f"/api/v1/crawl/jobs/{created['id']}/retry").json()["data"]

        assert new["crawler_type"] == "detail"
        assert new["params"]["ids"] == ["https://www.xiaohongshu.com/explore/a"]
        assert new["params"]["start_page"] == 3
        assert new["params"]["max_notes"] == 7
        assert new["params"]["get_comments"] is False

    def test_unknown_job_is_404(self, client):
        assert client.post("/api/v1/crawl/jobs/9999/retry").status_code == 404

    def test_env_broken_fails_fast_without_creating_a_job(self, client, monkeypatch):
        """MC 被挪走时点击即 400，且不留下半条任务记录。"""
        created = _create_job(client)
        monkeypatch.setattr(
            "app.services.crawl_job_service.probe_environment",
            lambda refresh=False: _stub_env_payload(installed=False, ready=False),
        )

        response = client.post(f"/api/v1/crawl/jobs/{created['id']}/retry")
        assert response.status_code == 400, response.text
        assert "MediaCrawler" in response.json()["error"]["message"]
        assert client.get("/api/v1/crawl/jobs").json()["data"]["total"] == 1


class TestJobRemark:
    """任务备注：用户自己看的标记（PUT /jobs/{id}/remark）。

    字段名是 remark 而不是 note —— note 在本域已经是「抓到的笔记」的意思。
    """

    def test_update_remark_echoed_in_detail_and_list(self, client):
        """备注写进去后，详情与列表都必须回显。"""
        created = _create_job(client)

        response = client.put(
            f"/api/v1/crawl/jobs/{created['id']}/remark", json={"remark": "  竞品参考  "}
        )
        assert response.status_code == 200, response.text
        # 首尾空白由 schema 统一剥掉，避免「看着是空的其实不是」
        assert response.json()["data"]["remark"] == "竞品参考"

        detail = client.get(f"/api/v1/crawl/jobs/{created['id']}")
        assert detail.json()["data"]["remark"] == "竞品参考"
        assert "note" not in detail.json()["data"], "备注不该挤掉既有的 note 语义"

        item = client.get("/api/v1/crawl/jobs").json()["data"]["items"][0]
        assert item["remark"] == "竞品参考"

    def test_empty_remark_clears_existing(self, client):
        """空串是「清空」而不是「不更新」—— 与 PATCH 的缺省语义刻意不同。"""
        created = _create_job(client)
        client.put(f"/api/v1/crawl/jobs/{created['id']}/remark", json={"remark": "写错了"})

        response = client.put(
            f"/api/v1/crawl/jobs/{created['id']}/remark", json={"remark": ""}
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["remark"] == ""
        detail = client.get(f"/api/v1/crawl/jobs/{created['id']}")
        assert detail.json()["data"]["remark"] == ""

    def test_remark_too_long_422(self, client):
        created = _create_job(client)
        response = client.put(
            f"/api/v1/crawl/jobs/{created['id']}/remark", json={"remark": "备" * 201}
        )
        assert response.status_code == 422, response.text
        # 超长被拒时备注保持原值，不写半截进去
        detail = client.get(f"/api/v1/crawl/jobs/{created['id']}")
        assert detail.json()["data"]["remark"] == ""

    def test_update_missing_job_404(self, client):
        response = client.put("/api/v1/crawl/jobs/9999/remark", json={"remark": "x"})
        assert response.status_code == 404, response.text
        assert response.json()["success"] is False


class TestNoteComments:
    """查看某条笔记的评论（GET /jobs/{id}/comments?note_id=）。"""

    def _job_with_comments(
        self, client, *, note_url: str = XHS_URL_WITH_TOKEN, with_comments: bool = True,
        **job_extra,
    ):
        """建一条任务并造出「一条笔记 + 一条评论带一个子评论」的产物。

        with_comments=False 用来复现「没开评论采集」的真实形态：评论文件
        **根本不存在**（不是空文件）—— MC 的 jsonl writer 是按需 append 打开的。
        """
        created = _create_job(client, **job_extra)
        _write_jsonl(
            Path(created["output_dir"]), "xhs", "contents",
            [mc_note("n1", note_url=note_url)],
        )
        if not with_comments:
            return created
        _write_jsonl(
            Path(created["output_dir"]), "xhs", "comments",
            [
                mc_comment("c1", note_id="n1", sub_comment_count=1, content="父评论"),
                mc_comment(
                    "c2", note_id="n1", parent_comment_id="c1", content="子评论"
                ),
            ],
        )
        return created

    def test_returns_tree_with_config(self, client):
        created = self._job_with_comments(client, max_comments=20, get_comments=True)

        response = client.get(
            f"/api/v1/crawl/jobs/{created['id']}/comments", params={"note_id": "n1"}
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]

        assert data["job_id"] == created["id"]
        assert data["note_id"] == "n1"
        assert data["platform"] == "xhs"
        # 总数含子评论，一级只有一条 —— 弹窗标题与「本地已抓 N 条」用这两个数
        assert data["total"] == 2
        assert data["top_level_total"] == 1
        parent = data["comments"][0]
        assert parent["content"] == "父评论"
        assert [child["content"] for child in parent["children"]] == ["子评论"]
        assert data["comments_config"] == {
            "enabled": True,
            "max_comments": 20,
            "sub_comments": False,
        }
        # 登录方式 / 无头是补抓设置的默认选中项（前端弹窗据此初始化单选与开关）
        assert data["login_type"] == "qrcode"
        assert data["headless"] is False
        assert data["refetch"] is None  # 没补抓过

    def test_comments_not_collected_marks_config_disabled(self, client):
        """原任务没开评论开关：产物里根本没有评论文件，这是正常态不是错误。"""
        created = self._job_with_comments(client, get_comments=False, with_comments=False)

        data = client.get(
            f"/api/v1/crawl/jobs/{created['id']}/comments", params={"note_id": "n1"}
        ).json()["data"]
        assert data["comments"] == []
        assert data["total"] == 0
        assert data["comments_config"]["enabled"] is False

    def test_zero_max_comments_counts_as_disabled(self, client):
        """只开了 get_comments 但 max_comments=0 时 MC 一条都不抓，不能算「开了」。"""
        created = self._job_with_comments(client, get_comments=True, max_comments=0)

        data = client.get(
            f"/api/v1/crawl/jobs/{created['id']}/comments", params={"note_id": "n1"}
        ).json()["data"]
        assert data["comments_config"]["enabled"] is False

    def test_note_not_in_results_404(self, client):
        created = self._job_with_comments(client)
        response = client.get(
            f"/api/v1/crawl/jobs/{created['id']}/comments", params={"note_id": "nope"}
        )
        assert response.status_code == 404, response.text

    def test_unknown_job_404(self, client):
        response = client.get(
            "/api/v1/crawl/jobs/9999/comments", params={"note_id": "n1"}
        )
        assert response.status_code == 404, response.text

    def test_derived_job_id_resolves_to_root(self, client):
        """手上拿着派生任务 id 也查得到同一条笔记的评论（往根任务解析）。"""
        created = self._job_with_comments(client)
        derived = client.post(
            f"/api/v1/crawl/jobs/{created['id']}/comments/refetch",
            json={"note_id": "n1", "max_comments": 20, "sub_comments": True},
        ).json()["data"]

        data = client.get(
            f"/api/v1/crawl/jobs/{derived['id']}/comments", params={"note_id": "n1"}
        ).json()["data"]

        assert data["job_id"] == created["id"]  # 回到根任务
        assert data["refetch"]["job_id"] == derived["id"]
        assert data["refetch"]["status"] == "pending"

    def test_refetch_state_reports_comment_count_and_queue(self, client):
        """补抓任务的状态块：评论条数单独数（不能用 note_count 顶），排队数如实。"""
        created = self._job_with_comments(client)
        derived = client.post(
            f"/api/v1/crawl/jobs/{created['id']}/comments/refetch",
            json={"note_id": "n1", "max_comments": 20, "sub_comments": True},
        ).json()["data"]
        # 造出这次补抓自己的评论产物（补抓任务的 note_count 数的是内容行，恒为 1）
        _write_jsonl(
            Path(derived["output_dir"]), "xhs", "comments",
            [mc_comment("c9", note_id="n1", content="补抓到的评论")],
        )

        refetch = client.get(
            f"/api/v1/crawl/jobs/{created['id']}/comments", params={"note_id": "n1"}
        ).json()["data"]["refetch"]

        assert refetch["comment_count"] == 1
        # 根任务还挂在 pending，派生任务排在它后面
        assert refetch["queued_ahead"] == 1

    def test_failure_gets_a_readable_hint_above_the_log(self, client, db_session):
        """补抓失败时先给一句中文原因，原始日志留在后面（实测的失败形态）。"""
        from app.models.crawl_job import CrawlJob, CrawlJobStatus

        created = self._job_with_comments(client)
        derived = client.post(
            f"/api/v1/crawl/jobs/{created['id']}/comments/refetch",
            json={"note_id": "n1", "max_comments": 20, "sub_comments": True},
        ).json()["data"]
        job = db_session.get(CrawlJob, derived["id"])
        job.status = CrawlJobStatus.FAILED
        job.error_message = (
            "MediaCrawler WARNING (core.py:317) - [skip] Failed to get note detail, "
            "Id: 68b8ec1a000000001b020598, 跳过继续"
        )
        db_session.commit()

        message = client.get(
            f"/api/v1/crawl/jobs/{created['id']}/comments", params={"note_id": "n1"}
        ).json()["data"]["refetch"]["error_message"]

        assert message.startswith("小红书没有返回这条笔记的详情")
        assert "Failed to get note detail" in message  # 原始日志没丢

    def test_parse_failure_gets_its_own_hint(self, client, db_session):
        """解析失败与「笔记抓不到」是两回事，话术不能互相串。

        MC 侧 extract_note_detail_from_html 读不懂页面时会抛带上下文的错误
        （页面拿到了，是抓取器读不懂），core.py 记一行 [skip] failed to parse
        后跳过这一条。用户此时最不该看到的是「笔记可能已被删除 / 换成扫码」——
        换扫码和重试都不会好。两个指纹同现时必须让解析那条赢（顺序即优先级）。
        """
        from app.models.crawl_job import CrawlJob, CrawlJobStatus

        created = self._job_with_comments(client)
        derived = client.post(
            f"/api/v1/crawl/jobs/{created['id']}/comments/refetch",
            json={"note_id": "n1", "max_comments": 20, "sub_comments": True},
        ).json()["data"]
        job = db_session.get(CrawlJob, derived["id"])
        job.status = CrawlJobStatus.FAILED
        job.error_message = (
            "MediaCrawler WARNING (core.py:314) - [skip] Failed to parse note detail html, "
            "Id: 68b8ec1a000000001b020598, Failed to parse window.__INITIAL_STATE__ at char "
            "32775: Expecting value. Context: ...{\"noteDetailMap\":{\"a\":new Set([...])}...\n"
            "MediaCrawler WARNING (core.py:320) - [skip] Failed to get note detail, "
            "Id: 68b8ec1a000000001b020598, 跳过继续"
        )
        db_session.commit()

        message = client.get(
            f"/api/v1/crawl/jobs/{created['id']}/comments", params={"note_id": "n1"}
        ).json()["data"]["refetch"]["error_message"]

        assert message.startswith("小红书改了笔记页面的数据格式")
        assert "换扫码" in message  # 明确否认「换登录方式能好」这条路
        assert "Failed to parse window.__INITIAL_STATE__ at char 32775" in message  # 现场没丢

    def test_pictures_are_localized_and_served_via_media(self, client, monkeypatch):
        """评论图是时效 URL：接口要把下到的换成缓存路径，且 /media 取得到。"""
        from app.services import crawl_comments

        monkeypatch.setattr(
            crawl_comments,
            "_http_get_picture",
            lambda url: (200, b"picture-bytes", "image/webp"),
        )
        created = self._job_with_comments(client)
        _write_jsonl(
            Path(created["output_dir"]), "xhs", "comments",
            [
                mc_comment(
                    "c3", note_id="n1", pictures="https://cdn.example.com/a.webp"
                )
            ],
        )

        data = client.get(
            f"/api/v1/crawl/jobs/{created['id']}/comments", params={"note_id": "n1"}
        ).json()["data"]
        comment = next(c for c in data["comments"] if c["id"] == "c3")
        relative = comment["pictures"][0]
        assert not relative.startswith("http")
        assert f"/{crawl_comments.COMMENT_MEDIA_DIR}/" in relative

        media = client.get(
            f"/api/v1/crawl/jobs/{created['id']}/media/{relative}"
        )
        assert media.status_code == 200, media.text
        assert media.content == b"picture-bytes"


class TestCommentRefetch:
    """对单条笔记补抓评论（POST /jobs/{id}/comments/refetch）。"""

    def _job_with_note(self, client, *, note_url: str = XHS_URL_WITH_TOKEN, **job_extra):
        created = _create_job(client, **job_extra)
        _write_jsonl(
            Path(created["output_dir"]), "xhs", "contents",
            [mc_note("n1", note_url=note_url)],
        )
        return created

    def _refetch(self, client, job_id: int, **payload):
        body = {"note_id": "n1", "max_comments": 50, "sub_comments": True}
        body.update(payload)
        return client.post(f"/api/v1/crawl/jobs/{job_id}/comments/refetch", json=body)

    def test_creates_detail_job_with_chosen_counts(self, client):
        """派生任务的参数：detail 模式、只抓这一条、条数与二级开关取用户所选。"""
        created = self._job_with_note(client, get_comments=False, max_comments=0)

        response = self._refetch(client, created["id"])
        assert response.status_code == 201, response.text
        derived = response.json()["data"]

        assert derived["crawler_type"] == "detail"
        assert derived["status"] == "pending"
        assert derived["params"]["ids"] == [XHS_URL_WITH_TOKEN]
        assert derived["params"]["max_notes"] == 1
        assert derived["params"]["get_comments"] is True
        assert derived["params"]["get_sub_comments"] is True
        # 关键：不继承原任务的 max_comments=0（那样等于抓 0 条还报 success）
        assert derived["params"]["max_comments"] == 50
        assert derived["expected_count"] == 1
        # 输出目录照旧一任务一个
        assert derived["output_dir"].endswith(f"job_{derived['id']}")

    def test_inherits_login_and_headless(self, client, db_session):
        """cookie 只存在于数据库行里，必须服务端继承；无头也跟着原任务走。"""
        from app.models.crawl_job import CrawlJob

        created = self._job_with_note(
            client, login_type="cookie", cookies=" a=1;b=2 ", headless=True
        )

        derived = self._refetch(client, created["id"]).json()["data"]

        assert derived["login_type"] == "cookie"
        assert "cookies" not in derived and "login_cookies" not in derived
        assert db_session.get(CrawlJob, derived["id"]).login_cookies == "a=1;b=2"
        assert derived["params"]["headless"] is True

    def test_login_type_and_headless_can_be_overridden(self, client, db_session):
        """cookie 过期后改选扫码：派生任务不带旧 cookie，无头也按新选择走。"""
        from app.models.crawl_job import CrawlJob

        created = self._job_with_note(
            client, login_type="cookie", cookies="a=1", headless=True
        )

        response = self._refetch(
            client, created["id"], login_type="qrcode", headless=False
        )
        assert response.status_code == 201, response.text
        derived = response.json()["data"]

        assert derived["login_type"] == "qrcode"
        assert derived["params"]["headless"] is False
        # 换扫码时旧串不能带进新任务（扫码与 cookie 是互斥的登录路径）
        assert db_session.get(CrawlJob, derived["id"]).login_cookies == ""

    def test_cookie_login_can_paste_a_fresh_cookie(self, client, db_session):
        """重新登录后贴了新 cookie：覆盖原任务存的旧串，且首尾空白被剥掉。"""
        from app.models.crawl_job import CrawlJob

        created = self._job_with_note(client, login_type="cookie", cookies="old=1")

        derived = self._refetch(
            client, created["id"], login_type="cookie", cookies=" new=2 "
        ).json()["data"]

        assert db_session.get(CrawlJob, derived["id"]).login_cookies == "new=2"

    def test_cookie_selected_without_pasting_reuses_the_stored_one(self, client, db_session):
        """选 cookie 但输入框留空 = 沿用原任务存的串（前端输入框永远是空的）。"""
        from app.models.crawl_job import CrawlJob

        created = self._job_with_note(client, login_type="cookie", cookies="a=1")

        derived = self._refetch(client, created["id"], login_type="cookie").json()["data"]

        assert db_session.get(CrawlJob, derived["id"]).login_cookies == "a=1"

    def test_cookie_login_without_any_cookie_returns_400(self, client):
        """根任务是扫码的，改选 cookie 又不贴串：点击时报，不排队。"""
        created = self._job_with_note(client)  # 默认扫码，库里的串是空

        response = self._refetch(client, created["id"], login_type="cookie")
        assert response.status_code == 400, response.text
        assert "cookie" in response.json()["error"]["message"].lower()
        # 没建出任务
        assert client.get("/api/v1/crawl/jobs").json()["data"]["total"] == 1

    def test_unknown_login_type_returns_422(self, client):
        created = self._job_with_note(client)
        response = self._refetch(client, created["id"], login_type="phone")
        assert response.status_code == 422, response.text

    def test_derived_job_is_hidden_from_history(self, client):
        """派生任务不是一次独立抓取：历史列表里看不到，且 total 与实际条数一致。"""
        created = self._job_with_note(client)
        self._refetch(client, created["id"])

        listing = client.get("/api/v1/crawl/jobs").json()["data"]
        assert listing["total"] == 1
        assert [item["id"] for item in listing["items"]] == [created["id"]]

        # 状态过滤同理：不能出现「total 说有、items 里没有」的空白页
        only_pending = client.get(
            "/api/v1/crawl/jobs", params={"status": "pending"}
        ).json()["data"]
        assert only_pending["total"] == len(only_pending["items"]) == 1

    def test_bilibili_is_refused_before_queuing(self, client):
        """B 站产物里只有 av 号、MC 只认 BV —— 点按钮就 400，不排注定失败的任务。"""
        created = _create_job(client, platform="bili", keywords=["数码"])
        _write_jsonl(
            Path(created["output_dir"]), "bili", "contents",
            [{"video_id": "n1", "title": "标题", "video_url": "https://www.bilibili.com/video/av12345"}],
        )

        response = self._refetch(client, created["id"])
        assert response.status_code == 400, response.text
        assert "BV" in response.json()["error"]["message"]
        # 没建出任务
        assert client.get("/api/v1/crawl/jobs").json()["data"]["total"] == 1

    def test_xhs_without_token_is_refused(self, client):
        """链接缺 xsec_token 时补抓必然 0 结果，提前拦掉并告诉用户怎么办。"""
        created = self._job_with_note(
            client, note_url="https://www.xiaohongshu.com/explore/n1"
        )

        response = self._refetch(client, created["id"])
        assert response.status_code == 400, response.text
        assert "xsec_token" in response.json()["error"]["message"]

    def test_note_not_in_results_404(self, client):
        created = self._job_with_note(client)
        response = self._refetch(client, created["id"], note_id="nope")
        assert response.status_code == 404, response.text

    def test_unknown_job_404(self, client):
        response = client.post(
            "/api/v1/crawl/jobs/9999/comments/refetch",
            json={"note_id": "n1", "max_comments": 20, "sub_comments": True},
        )
        assert response.status_code == 404, response.text

    def test_live_refetch_conflicts_with_existing_job_id(self, client):
        """同一条笔记同时只允许一个补抓在跑；409 带上已存在那条的 id（前端据此幂等）。"""
        created = self._job_with_note(client)
        first = self._refetch(client, created["id"]).json()["data"]

        response = self._refetch(client, created["id"])
        assert response.status_code == 409, response.text
        assert response.json()["error"]["details"]["job_id"] == first["id"]

    def test_can_refetch_again_after_cancel(self, client):
        """上一条补抓结束了就不算「正在补抓」，可以再来一次。"""
        created = self._job_with_note(client)
        first = self._refetch(client, created["id"]).json()["data"]
        client.post(f"/api/v1/crawl/jobs/{first['id']}/cancel")

        response = self._refetch(client, created["id"])
        assert response.status_code == 201, response.text
        assert response.json()["data"]["id"] != first["id"]

    def test_deleting_root_cascades_derived(self, client, db_session):
        """删原任务连带删派生任务（补抓不是独立任务，留着就是孤儿行）。"""
        from app.models.crawl_job import CrawlJob

        created = self._job_with_note(client)
        derived = self._refetch(client, created["id"]).json()["data"]

        client.post(f"/api/v1/crawl/jobs/{created['id']}/cancel")
        response = client.delete(f"/api/v1/crawl/jobs/{created['id']}")
        assert response.status_code == 200, response.text

        assert db_session.get(CrawlJob, created["id"]) is None
        assert db_session.get(CrawlJob, derived["id"]) is None

    def test_deleting_root_kills_a_running_refetch(self, client, db_session, monkeypatch):
        """补抓还在跑时删原任务：光删行不够，必须同步杀掉 MC 子进程。

        行一删，runner 的 `_is_cancelled` 读不到行会一直返回 False，`_finalize`
        里 `db.get` 又是 None 直接 return —— MC 与它拉起的 CDP Chrome 会变成
        没人管的孤儿，还在往已被清掉的输出目录里写数据。
        """
        from app.models.crawl_job import CrawlJob, CrawlJobStatus

        killed = []
        monkeypatch.setattr(
            "app.services.crawl_job_service.is_our_child", lambda pid: True
        )
        monkeypatch.setattr(
            "app.services.crawl_job_service.terminate_process_group",
            lambda pid, grace: killed.append((pid, grace)),
        )

        created = self._job_with_note(client)
        derived = self._refetch(client, created["id"]).json()["data"]
        row = db_session.get(CrawlJob, derived["id"])
        row.status = CrawlJobStatus.RUNNING
        row.child_pid = 424242
        db_session.commit()

        client.post(f"/api/v1/crawl/jobs/{created['id']}/cancel")
        assert client.delete(f"/api/v1/crawl/jobs/{created['id']}").status_code == 200

        assert killed and killed[0][0] == 424242
        assert db_session.get(CrawlJob, derived["id"]) is None

    def test_derived_products_are_purged_with_root(self, client):
        """purge_files 时派生任务的输出目录也要清 —— 否则留一堆没人认领的目录。"""
        created = self._job_with_note(client)
        derived = self._refetch(client, created["id"]).json()["data"]
        derived_dir = Path(derived["output_dir"])
        derived_dir.mkdir(parents=True, exist_ok=True)
        (derived_dir / "leftover.jsonl").write_text("{}", encoding="utf-8")

        client.post(f"/api/v1/crawl/jobs/{created['id']}/cancel")
        response = client.delete(f"/api/v1/crawl/jobs/{created['id']}?purge_files=true")
        assert response.status_code == 200, response.text
        assert not derived_dir.exists()
