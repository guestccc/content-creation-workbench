"""视频字幕提取接口测试：信封格式、输入校验、状态机与字幕预览。

执行层（子进程、结果判定）的测试在 test_subtitle_runner.py，
探测逻辑的测试在 test_subtitle_env.py —— 这里只钉接口层的契约。
"""

from pathlib import Path

import pytest

from app.core.config import settings
from app.services import subtitle_settings
from app.services.subtitle_env import VcInstall
from tests.fakes import DEFAULT_SRT


@pytest.fixture(autouse=True)
def _isolate_vc_settings(monkeypatch, tmp_path):
    """把「手动指定 VideoCaptioner 目录」涉及的持久化与运行态全隔离掉。

    - `.env` 指到 tmp：那个接口会**真的读写**这个文件，绝不能碰开发机上的
      backend/.env（里面是用户自己的配置）；
    - settings 单例做快照：接口里的赋值是应用代码的裸赋值，monkeypatch
      拦不住写入本身，只能靠 teardown 恢复；
    - 清掉可能存在的同名环境变量：它会盖过 .env，让「写进去了没有」类的
      断言取决于开发机的 shell。
    """
    monkeypatch.setattr(subtitle_settings, "_ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("SUBTITLE_VC_ROOT", raising=False)
    original_root = settings.SUBTITLE_VC_ROOT
    yield
    settings.SUBTITLE_VC_ROOT = original_root


@pytest.fixture()
def video_dir(tmp_path):
    """造一个含两条假视频的输入目录，返回 (目录, 视频名列表)。"""
    source = tmp_path / "素材"
    source.mkdir()
    (source / "口播A.mp4").write_bytes(b"fake-video-a")
    (source / "口播B.mp4").write_bytes(b"fake-video-b")
    (source / "说明.txt").write_text("不是视频", encoding="utf-8")
    return source, ["口播A.mp4", "口播B.mp4"]


def _create_job(client, source, output, **extra) -> dict:
    """创建一个任务并返回响应 data。"""
    payload = {"input_path": str(source), "output_dir": str(output), **extra}
    response = client.post("/api/v1/subtitle/jobs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


class TestEnvironment:
    """环境自检接口（静态路由 /environment 不能被 /{job_id} 吞掉）。"""

    def test_environment_envelope(self, client, monkeypatch):
        """信封格式 + 指引与引擎清单都在 data 里。"""
        monkeypatch.setattr(
            "app.api.v1.subtitle_jobs.probe_environment",
            lambda refresh=False: {
                "installed": False,
                "ready": False,
                "launcher": [],
                "kind": "",
                "root": "",
                "version": "",
                "python_version": "",
                "config_file": "",
                "config_exists": False,
                "ffmpeg_path": "",
                "detail": "没有找到可用的 VideoCaptioner",
                "platform": "macos",
                "platform_label": "macOS",
                "python_platform": "test",
                "materials_dir": "/tmp/materials",
                "default_input_dir": "/tmp/materials/source",
                "default_output_dir": "/tmp/materials/subtitle",
                "install_hints": [{"title": "安装", "command": "pip install",
                                   "note": "", "url": ""}],
            },
        )
        response = client.get("/api/v1/subtitle/environment")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is True
        data = body["data"]
        assert data["installed"] is False
        assert data["install_hints"][0]["command"] == "pip install"
        assert data["asr_engines"]  # 引擎清单给前端渲染选项用

    def test_environment_refresh_passthrough(self, client, monkeypatch):
        """?refresh=1 必须传到探测层（「重新检测」按钮靠它绕过缓存）。"""
        seen = []

        def fake_probe(refresh=False):
            seen.append(refresh)
            return {
                "installed": True, "ready": True,
                "launcher": ["/py", "-m", "videocaptioner"], "kind": "venv-python",
                "root": "", "version": "1.0.0", "python_version": "3.11",
                "config_file": "", "config_exists": False,
                "ffmpeg_path": "/usr/bin/ffmpeg", "detail": "",
                "platform": "macos", "platform_label": "macOS",
                "python_platform": "test", "materials_dir": "/tmp/m",
                "default_input_dir": "/tmp/m/source",
                "default_output_dir": "/tmp/m/subtitle",
                "install_hints": [],
            }

        monkeypatch.setattr("app.api.v1.subtitle_jobs.probe_environment", fake_probe)
        client.get("/api/v1/subtitle/environment")
        client.get("/api/v1/subtitle/environment", params={"refresh": True})
        assert seen == [False, True]


def _stub_probe(monkeypatch, **overrides):
    """把探测层换成固定的返回，只钉接口层的写盘与校验行为。

    不跑真探测：那样会起子进程、扫描开发机的真实目录，既慢又让断言依赖本机。
    """
    payload = {
        "installed": False, "ready": False, "launcher": [], "kind": "",
        "root": "", "version": "", "python_version": "", "config_file": "",
        "config_exists": False, "ffmpeg_path": "", "detail": "",
        "platform": "windows", "platform_label": "Windows",
        "python_platform": "test", "materials_dir": "/tmp/m",
        "default_input_dir": "/tmp/m/source", "default_output_dir": "/tmp/m/subtitle",
        "install_hints": [], "vc_root_source": "env_file", "vc_search_dir": "/tmp",
        "warnings": [],
    }
    payload.update(overrides)
    monkeypatch.setattr(
        "app.api.v1.subtitle_jobs.probe_environment", lambda refresh=False: dict(payload)
    )


class TestSetVcRoot:
    """手动指定 VideoCaptioner 目录（PUT /environment/vc-root）。"""

    def test_writes_value_and_reports_back(self, client, monkeypatch, tmp_path):
        """选定目录 → 写进 .env → 当前进程立刻生效。"""
        target = tmp_path / "VideoCaptioner-master"
        (target / ".venv").mkdir(parents=True)
        _stub_probe(monkeypatch, installed=True, ready=True)

        response = client.put(
            "/api/v1/subtitle/environment/vc-root", json={"path": str(target)}
        )

        assert response.status_code == 200, response.text
        assert subtitle_settings.read_vc_root() == str(target)
        # 不等重启就生效，靠的是原地改 settings 单例
        assert Path(settings.SUBTITLE_VC_ROOT) == target

    def test_empty_path_resets_to_auto(self, client, monkeypatch, tmp_path):
        """空串 = 清除指定，恢复自动探测。"""
        target = tmp_path / "VideoCaptioner"
        target.mkdir()
        _stub_probe(monkeypatch)
        client.put("/api/v1/subtitle/environment/vc-root", json={"path": str(target)})
        assert subtitle_settings.read_vc_root()  # 先确认真的存进去了

        response = client.put(
            "/api/v1/subtitle/environment/vc-root", json={"path": ""}
        )

        assert response.status_code == 200, response.text
        assert subtitle_settings.read_vc_root() == ""
        assert settings.SUBTITLE_VC_ROOT == ""

    def test_missing_path_returns_400(self, client, monkeypatch, tmp_path):
        """路径不存在 → 400，且不写盘。"""
        _stub_probe(monkeypatch)
        response = client.put(
            "/api/v1/subtitle/environment/vc-root",
            json={"path": str(tmp_path / "并没有这个目录")},
        )
        assert response.status_code == 400
        assert subtitle_settings.read_vc_root() is None

    def test_file_path_returns_400(self, client, monkeypatch, tmp_path):
        """给的是文件而不是目录 → 400。"""
        a_file = tmp_path / "videocaptioner.exe"
        a_file.write_text("x", encoding="utf-8")
        _stub_probe(monkeypatch)

        response = client.put(
            "/api/v1/subtitle/environment/vc-root", json={"path": str(a_file)}
        )
        assert response.status_code == 400

    def test_newline_in_path_returns_400(self, client, monkeypatch, tmp_path):
        """路径里塞换行能往 .env 追加任意配置键，是一次配置注入。"""
        _stub_probe(monkeypatch)
        response = client.put(
            "/api/v1/subtitle/environment/vc-root",
            json={"path": "/opt/vc\nSUBTITLE_WORKER_ENABLED=false"},
        )
        assert response.status_code == 400
        assert subtitle_settings.read_vc_root() is None

    def test_unrelated_directory_is_allowed_with_warning(
        self, client, monkeypatch, tmp_path
    ):
        """选了不像 VideoCaptioner 的目录：照常接受，只给提醒。

        严格的「必须像 VideoCaptioner」校验正是这次探测不到的原因，
        不能再拿它当门槛挡用户。
        """
        plain = tmp_path / "随便一个目录"
        plain.mkdir()
        _stub_probe(monkeypatch)

        response = client.put(
            "/api/v1/subtitle/environment/vc-root", json={"path": str(plain)}
        )

        assert response.status_code == 200, response.text
        assert Path(settings.SUBTITLE_VC_ROOT) == plain
        warnings = response.json()["data"]["warnings"]
        assert any("videocaptioner" in text for text in warnings)

    def test_looks_like_vc_directory_gets_no_extra_warning(
        self, client, monkeypatch, tmp_path
    ):
        """目录里确实有 .venv 时不该多嘴。"""
        target = tmp_path / "VideoCaptioner"
        (target / ".venv").mkdir(parents=True)
        _stub_probe(monkeypatch, installed=True, ready=True)

        response = client.put(
            "/api/v1/subtitle/environment/vc-root", json={"path": str(target)}
        )
        assert response.json()["data"]["warnings"] == []

    def test_warnings_from_probe_layer_pass_through(
        self, client, monkeypatch, tmp_path
    ):
        """探测层给出的提醒要原样传给前端。

        典型场景是环境变量盖过了 .env：那句话由 subtitle_settings 生成、
        经 probe_environment 带出来，这里只钉「有没有传到」。文案本身在
        test_subtitle_settings.py 里测。
        """
        target = tmp_path / "VideoCaptioner"
        (target / ".venv").mkdir(parents=True)
        _stub_probe(monkeypatch, warnings=["系统环境变量里已经设置了 SUBTITLE_VC_ROOT"])

        response = client.put(
            "/api/v1/subtitle/environment/vc-root", json={"path": str(target)}
        )

        assert response.status_code == 200
        warnings = " ".join(response.json()["data"]["warnings"])
        assert "环境变量" in warnings

    def test_tilde_is_expanded(self, client, monkeypatch):
        """带 ~ 的路径要展开后再落盘，否则写进去的是字面量。"""
        _stub_probe(monkeypatch)
        home = Path.home()

        response = client.put(
            "/api/v1/subtitle/environment/vc-root", json={"path": "~"}
        )

        assert response.status_code == 200, response.text
        assert "~" not in (subtitle_settings.read_vc_root() or "")
        assert Path(settings.SUBTITLE_VC_ROOT) == home

    def test_route_is_not_swallowed_by_job_id(self, client, monkeypatch):
        """静态路径不能被 /jobs/{job_id} 之类吞掉（本文件顶部的路由契约）。"""
        _stub_probe(monkeypatch)
        response = client.get("/api/v1/subtitle/environment")
        assert response.status_code == 200
        # PUT 到 /jobs/{id} 不存在的路由应该 405，而不是走到 job 详情上
        assert client.put("/api/v1/subtitle/jobs/1", json={}).status_code == 405


class TestCreateJob:
    """创建任务的校验与信封。"""

    def test_create_enumerates_videos(self, client, video_dir, tmp_path):
        source, names = video_dir
        data = _create_job(client, source, tmp_path / "字幕")

        assert data["status"] == "pending"
        assert data["total_videos"] == 2
        assert [item["source_name"] for item in data["items"]] == names
        for item in data["items"]:
            assert item["output_path"].endswith(".srt")
        # 默认引擎是免费的 bijian
        assert data["params"]["asr"] == "bijian"
        assert data["params"]["format"] == "srt"

    def test_create_with_file_selection(self, client, video_dir, tmp_path):
        source, _ = video_dir
        data = _create_job(client, source, tmp_path / "字幕", files=["口播B.mp4"])
        assert data["total_videos"] == 1
        assert data["items"][0]["source_name"] == "口播B.mp4"

    def test_not_installed_returns_400(self, client, video_dir, tmp_path, monkeypatch):
        """未装 VideoCaptioner → 400 且带可读原因（失败要快，不排队）。"""
        monkeypatch.setattr(
            "app.services.subtitle_job_service.detect",
            lambda *a, **k: VcInstall(installed=False, detail="没找到"),
        )
        source, _ = video_dir
        response = client.post(
            "/api/v1/subtitle/jobs",
            json={"input_path": str(source), "output_dir": str(tmp_path / "字幕")},
        )
        assert response.status_code == 400, response.text
        body = response.json()
        assert body["success"] is False
        assert "VideoCaptioner" in body["error"]["message"]

    def test_files_with_separator_rejected(self, client, video_dir, tmp_path):
        """勾选文件名带路径分隔符 → 422（防越出输入目录）。"""
        source, _ = video_dir
        response = client.post(
            "/api/v1/subtitle/jobs",
            json={
                "input_path": str(source),
                "output_dir": str(tmp_path / "字幕"),
                "files": ["../别处/视频.mp4"],
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["success"] is False

    def test_unknown_asr_engine_rejected(self, client, video_dir, tmp_path):
        """CLI 不认的引擎（比如文档写了但没实现的 faster-whisper）→ 422。"""
        source, _ = video_dir
        response = client.post(
            "/api/v1/subtitle/jobs",
            json={
                "input_path": str(source),
                "output_dir": str(tmp_path / "字幕"),
                "asr": "faster-whisper",
            },
        )
        assert response.status_code == 422, response.text

    def test_missing_input_returns_400(self, client, tmp_path):
        response = client.post(
            "/api/v1/subtitle/jobs",
            json={"input_path": str(tmp_path / "不存在"), "output_dir": str(tmp_path)},
        )
        assert response.status_code == 400, response.text


class TestListAndDetail:
    """列表与详情。"""

    def test_list_and_detail(self, client, video_dir, tmp_path):
        source, _ = video_dir
        created = _create_job(client, source, tmp_path / "字幕")

        response = client.get("/api/v1/subtitle/jobs")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == created["id"]
        # 列表不带每条视频的明细（详情接口才带）
        assert not data["items"][0]["items"]

        detail = client.get(f"/api/v1/subtitle/jobs/{created['id']}")
        assert detail.status_code == 200
        assert len(detail.json()["data"]["items"]) == 2

    def test_get_missing_job_returns_404(self, client):
        response = client.get("/api/v1/subtitle/jobs/9999")
        assert response.status_code == 404
        assert response.json()["success"] is False

    def test_status_filter(self, client, video_dir, tmp_path):
        source, _ = video_dir
        created = _create_job(client, source, tmp_path / "字幕")
        client.post(f"/api/v1/subtitle/jobs/{created['id']}/cancel")

        cancelled = client.get("/api/v1/subtitle/jobs", params={"status": "cancelled"})
        assert cancelled.json()["data"]["total"] == 1
        pending = client.get("/api/v1/subtitle/jobs", params={"status": "pending"})
        assert pending.json()["data"]["total"] == 0


class TestSubtitlesEndpoints:
    """字幕清单与文本预览。"""

    def _create_with_one_produced(self, client, video_dir, tmp_path):
        """建一个任务并「假装」第一条字幕已产出，返回 (任务 data, 产物路径)。"""
        source, _ = video_dir
        data = _create_job(client, source, tmp_path / "字幕")
        produced = Path(data["items"][0]["output_path"])
        produced.write_text(DEFAULT_SRT, encoding="utf-8")
        return data, produced

    def test_list_only_returns_existing(self, client, video_dir, tmp_path):
        """清单只返回已落盘的（进度看任务详情的条目状态）。"""
        data, produced = self._create_with_one_produced(client, video_dir, tmp_path)
        response = client.get(f"/api/v1/subtitle/jobs/{data['id']}/subtitles")
        assert response.status_code == 200
        files = response.json()["data"]
        assert len(files) == 1
        assert files[0]["name"] == produced.name
        assert files[0]["source_name"] == "口播A.mp4"
        assert files[0]["size_bytes"] > 0

    def test_get_subtitle_text(self, client, video_dir, tmp_path):
        data, _ = self._create_with_one_produced(client, video_dir, tmp_path)
        response = client.get(f"/api/v1/subtitle/jobs/{data['id']}/subtitles/1")
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["content"] == DEFAULT_SRT
        assert body["truncated"] is False
        assert body["source_name"] == "口播A.mp4"

    def test_get_subtitle_text_truncates(self, client, video_dir, tmp_path, monkeypatch):
        """超过预览上限：截断 + truncated 标记，且结尾不剩半个字幕块。"""
        monkeypatch.setattr(settings, "SUBTITLE_PREVIEW_MAX_BYTES", 40)
        data, _ = self._create_with_one_produced(client, video_dir, tmp_path)

        response = client.get(f"/api/v1/subtitle/jobs/{data['id']}/subtitles/1")
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["truncated"] is True
        assert body["size_bytes"] > 40
        # 最后一个「块边界」之后的内容被砍掉了
        assert body["content"].endswith("\n") is False or "\n\n" not in body["content"][-2:]

    def test_get_subtitle_text_out_of_range(self, client, video_dir, tmp_path):
        data, _ = self._create_with_one_produced(client, video_dir, tmp_path)
        response = client.get(f"/api/v1/subtitle/jobs/{data['id']}/subtitles/99")
        assert response.status_code == 404

    def test_get_subtitle_text_not_generated(self, client, video_dir, tmp_path):
        """序号合法但字幕还没生成 → 404，而不是把路径交出去。"""
        data, _ = self._create_with_one_produced(client, video_dir, tmp_path)
        response = client.get(f"/api/v1/subtitle/jobs/{data['id']}/subtitles/2")
        assert response.status_code == 404
        assert response.json()["success"] is False


class TestCancelAndDelete:
    """状态机的接口面。"""

    def test_cancel_then_delete(self, client, video_dir, tmp_path):
        source, _ = video_dir
        data = _create_job(client, source, tmp_path / "字幕")

        response = client.post(f"/api/v1/subtitle/jobs/{data['id']}/cancel")
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "cancelled"

        deleted = client.delete(f"/api/v1/subtitle/jobs/{data['id']}")
        assert deleted.status_code == 200
        assert deleted.json()["data"] == {"id": data["id"]}

    def test_cancel_terminal_job_returns_409(self, client, video_dir, tmp_path):
        source, _ = video_dir
        data = _create_job(client, source, tmp_path / "字幕")
        client.post(f"/api/v1/subtitle/jobs/{data['id']}/cancel")
        response = client.post(f"/api/v1/subtitle/jobs/{data['id']}/cancel")
        assert response.status_code == 409

    def test_delete_running_job_returns_409(self, client, video_dir, tmp_path):
        source, _ = video_dir
        data = _create_job(client, source, tmp_path / "字幕")
        response = client.delete(f"/api/v1/subtitle/jobs/{data['id']}")
        assert response.status_code == 409
