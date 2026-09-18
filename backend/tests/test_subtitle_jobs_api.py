"""视频字幕提取接口测试：信封格式、输入校验、状态机与字幕预览。

执行层（子进程、结果判定）的测试在 test_subtitle_runner.py，
探测逻辑的测试在 test_subtitle_env.py —— 这里只钉接口层的契约。
"""

from pathlib import Path

import pytest

from app.core.config import settings
from app.services.subtitle_env import VcInstall
from tests.fakes import DEFAULT_SRT


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
