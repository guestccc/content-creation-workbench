"""素材抓取接口测试：信封格式、输入校验、状态机与产物读取。

执行层（子进程、结果判定）的测试在 test_crawl_runner.py，
产物归一化的测试在 test_crawl_results.py —— 这里只钉接口层的契约。
"""

import json
from pathlib import Path

import pytest

from app.core.config import settings
from tests.fakes import mc_note

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

    def test_qrcode_login_forces_headless_off(self, client):
        """扫码登录必须有界面：前端就算传了 headless 也要压回去。"""
        data = _create_job(client, headless=True)
        assert data["params"]["headless"] is False

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
        assert first["cover"] == first["images"][0]

    def test_results_with_local_media(self, client):
        data = _create_job(client)
        self._produce_notes(Path(data["output_dir"]), count=1)
        media = Path(data["output_dir"]) / "xhs" / "images" / "n1"
        media.mkdir(parents=True)
        (media / "1.webp").write_bytes(b"img-bytes")

        body = client.get(f"/api/v1/crawl/jobs/{data['id']}/results").json()["data"]
        assert body["notes"][0]["local_images"] == ["xhs/images/n1/1.webp"]

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
