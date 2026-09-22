"""视频字幕提取接口测试：信封格式、输入校验、状态机与字幕预览。

执行层（子进程、结果判定）的测试在 test_subtitle_runner.py，
探测逻辑的测试在 test_subtitle_env.py —— 这里只钉接口层的契约。
"""

from pathlib import Path

import pytest

from app.core.config import settings
from app.models.subtitle_job import (
    SubtitleJob,
    SubtitleJobItemStatus,
    SubtitleJobStatus,
)
from app.services import subtitle_settings
from app.services.subtitle_env import VcInstall
from app.services.subtitle_job_service import SubtitleJobService
from tests.fakes import DEFAULT_SRT, FakeVcPopen
# 执行层的夹具（假子进程 + 连测试库的执行器）在 test_subtitle_runner.py 里，
# 重试「真跑一遍」那条用例直接复用，免得在这里抄第二套假的 VideoCaptioner。
from tests.test_subtitle_runner import _make_job, _make_runner, _refresh


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

    def test_cancel_while_queued_marks_items_skipped(self, client, video_dir, tmp_path):
        """排队中就被取消：条目不能留在「等待中」。

        认领要求 status == pending，任务一旦置成 cancelled 就再也不会被认领，
        留在 pending 的条目既不会被执行、也不计入 failed / skipped。
        """
        source, _ = video_dir
        data = _create_job(client, source, tmp_path / "字幕")

        cancelled = client.post(f"/api/v1/subtitle/jobs/{data['id']}/cancel").json()["data"]
        assert cancelled["status"] == "cancelled"
        assert all(item["status"] == "skipped" for item in cancelled["items"])
        assert "已取消" in cancelled["items"][0]["error_message"]
        assert cancelled["skipped_videos"] == len(cancelled["items"])
        assert cancelled["failed_videos"] == 0 and cancelled["completed_videos"] == 0

    def test_cancelled_while_queued_can_be_retried(self, client, video_dir, tmp_path):
        """回归：排队中被取消的任务必须能单条重试。

        条目若留在 pending，重试会被 409 拒掉（「只有失败或跳过的条目才能重试」），
        而页面上一个重试入口都不会出现。
        """
        source, _ = video_dir
        data = _create_job(client, source, tmp_path / "字幕")
        client.post(f"/api/v1/subtitle/jobs/{data['id']}/cancel")

        response = client.post(f"/api/v1/subtitle/jobs/{data['id']}/items/1/retry")
        assert response.status_code == 200, response.text
        retried = response.json()["data"]
        assert retried["status"] == "pending"
        assert next(it for it in retried["items"] if it["index"] == 1)["status"] == "pending"

    def test_cancelled_while_queued_can_be_retried_all(self, client, video_dir, tmp_path):
        """回归：整批取消后「一键全部重试」要有候选，不能一条都找不到。"""
        source, _ = video_dir
        data = _create_job(client, source, tmp_path / "字幕")
        client.post(f"/api/v1/subtitle/jobs/{data['id']}/cancel")

        response = client.post(f"/api/v1/subtitle/jobs/{data['id']}/retry")
        assert response.status_code == 200, response.text
        retried = response.json()["data"]
        assert retried["status"] == "pending"
        assert all(item["status"] == "pending" for item in retried["items"])


class TestBatchDelete:
    """批量删除：整批成功或整批失败（POST /jobs/batch-delete）。"""

    def _create_terminal(self, client, video_dir, tmp_path, tag) -> int:
        source, _ = video_dir
        created = _create_job(client, source, tmp_path / f"输出{tag}")
        client.post(f"/api/v1/subtitle/jobs/{created['id']}/cancel")
        return created["id"]

    def test_batch_delete_success(self, client, video_dir, tmp_path):
        ids = [self._create_terminal(client, video_dir, tmp_path, tag) for tag in "ab"]

        response = client.post("/api/v1/subtitle/jobs/batch-delete", json={"ids": ids})
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"ids": ids, "count": 2}
        for job_id in ids:
            assert client.get(f"/api/v1/subtitle/jobs/{job_id}").status_code == 404

    def test_batch_delete_non_terminal_conflict_rolls_back(
        self, client, video_dir, tmp_path
    ):
        """批里混着一条未结束的：整批 409，已勾选的终态任务一条也不能被删。"""
        terminal_id = self._create_terminal(client, video_dir, tmp_path, "a")
        source, _ = video_dir
        pending = _create_job(client, source, tmp_path / "输出b")["id"]

        response = client.post(
            "/api/v1/subtitle/jobs/batch-delete", json={"ids": [terminal_id, pending]}
        )
        assert response.status_code == 409, response.text
        assert client.get(f"/api/v1/subtitle/jobs/{terminal_id}").status_code == 200

    def test_batch_delete_empty_ids_422(self, client):
        response = client.post("/api/v1/subtitle/jobs/batch-delete", json={"ids": []})
        assert response.status_code == 422, response.text


class TestDeletePurge:
    """删除任务时可选连产物一起清（purge_files）—— 字幕是「一条视频一份」的文件。"""

    def _terminal_with_products(self, client, video_dir, tmp_path) -> tuple[int, list[Path]]:
        """造一条终态任务，并按条目登记的路径各写一份假字幕文件。"""
        source, _ = video_dir
        created = _create_job(client, source, tmp_path / "字幕")
        files = []
        for item in created["items"]:
            path = Path(item["output_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("1\n00:00:00,000 --> 00:00:01,000\n测试\n", encoding="utf-8")
            files.append(path)
        client.post(f"/api/v1/subtitle/jobs/{created['id']}/cancel")
        return created["id"], files

    def test_delete_without_purge_keeps_products(self, client, video_dir, tmp_path):
        """默认只删记录：字幕文件原样保留。"""
        job_id, files = self._terminal_with_products(client, video_dir, tmp_path)

        response = client.delete(f"/api/v1/subtitle/jobs/{job_id}")
        assert response.status_code == 200, response.text
        for path in files:
            assert path.is_file(), f"字幕文件被误删：{path}"

    def test_delete_with_purge_removes_products(self, client, video_dir, tmp_path):
        """purge_files=true：各条目的字幕文件都清掉。"""
        job_id, files = self._terminal_with_products(client, video_dir, tmp_path)

        response = client.delete(f"/api/v1/subtitle/jobs/{job_id}?purge_files=true")
        assert response.status_code == 200, response.text
        for path in files:
            assert not path.exists(), f"字幕文件没清掉：{path}"


class TestUpdateRemark:
    """更新任务备注（PUT /jobs/{id}/remark）：纯用户标记，不参与状态机。"""

    def test_remark_echoed_in_detail_and_list(self, client, video_dir, tmp_path):
        """保存成功：详情接口与列表接口都回显新备注。"""
        source, _ = video_dir
        created = _create_job(client, source, tmp_path / "字幕")
        remark = "给客户 A 的那版"

        response = client.put(
            f"/api/v1/subtitle/jobs/{created['id']}/remark", json={"remark": remark}
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["remark"] == remark

        detail = client.get(f"/api/v1/subtitle/jobs/{created['id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["remark"] == remark

        # 列表走的是 from_model(include_items=False) 那条分支，容易漏掉 remark
        listed = client.get("/api/v1/subtitle/jobs")
        assert listed.status_code == 200, listed.text
        assert listed.json()["data"]["items"][0]["remark"] == remark

    def test_empty_remark_clears_existing(self, client, video_dir, tmp_path):
        """空串是有效值：把已有备注清掉，而不是被当成「不更新」。"""
        source, _ = video_dir
        created = _create_job(client, source, tmp_path / "字幕")
        url = f"/api/v1/subtitle/jobs/{created['id']}/remark"
        client.put(url, json={"remark": "先写一句"})

        response = client.put(url, json={"remark": ""})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["remark"] == ""

        detail = client.get(f"/api/v1/subtitle/jobs/{created['id']}")
        assert detail.json()["data"]["remark"] == ""

    def test_remark_over_limit_422(self, client, video_dir, tmp_path):
        """超过 200 字 → 422（上限与前端编辑弹窗的 maxLength 一致）。"""
        source, _ = video_dir
        created = _create_job(client, source, tmp_path / "字幕")

        response = client.put(
            f"/api/v1/subtitle/jobs/{created['id']}/remark", json={"remark": "备" * 201}
        )
        assert response.status_code == 422, response.text
        assert response.json()["success"] is False

    def test_update_remark_missing_job_returns_404(self, client):
        """任务不存在 → 404。"""
        response = client.put("/api/v1/subtitle/jobs/99999/remark", json={"remark": "x"})
        assert response.status_code == 404, response.text


def _settle(db_session, job_id: int, statuses: dict, *, job_status: str = "partial"):
    """把任务摆成「跑完了、但有几条没成」的样子。

    接口测试不真起 VideoCaptioner（会取决于开发机装没装），所以直接写库把每条
    的状态摆好；任务级计数按同一份 statuses 数一遍，与真跑完之后的记录形状
    一致 —— 重试的断言（尤其是计数重算）才有意义。
    """
    job = db_session.get(SubtitleJob, job_id)
    assert job is not None
    for item in job.items:
        item.status = statuses.get(item.index, SubtitleJobItemStatus.SUCCESS)
        if item.status == SubtitleJobItemStatus.SUCCESS:
            path = Path(item.output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_SRT, encoding="utf-8")
            item.subtitle_exists = True
            item.file_size = path.stat().st_size
            item.segment_count = 2
        else:
            item.error_message = f"第 {item.index} 条转写失败了"

    job.status = job_status
    job.completed_videos = len(job.items)
    job.failed_videos = sum(
        1 for it in job.items if it.status == SubtitleJobItemStatus.FAILED
    )
    job.skipped_videos = sum(
        1 for it in job.items if it.status == SubtitleJobItemStatus.SKIPPED
    )
    job.subtitle_count = sum(1 for it in job.items if it.subtitle_exists)
    db_session.commit()
    return job


class TestRetry:
    """单条重试：只有「没产出」的条目能重来，任务回到排队中。"""

    def test_failed_item_is_requeued(self, client, db_session, video_dir, tmp_path):
        source, _ = video_dir
        job = _create_job(client, source, tmp_path / "字幕")
        _settle(db_session, job["id"], {1: SubtitleJobItemStatus.FAILED})

        response = client.post(f"/api/v1/subtitle/jobs/{job['id']}/items/1/retry")
        assert response.status_code == 200, response.text
        data = response.json()["data"]

        assert data["status"] == "pending"
        assert data["finished_at"] is None
        assert data["error_message"] == ""
        item = next(it for it in data["items"] if it["index"] == 1)
        assert item["status"] == "pending"
        assert item["error_message"] == ""
        assert item["subtitle_exists"] is False
        assert item["segment_count"] == 0

    def test_skipped_item_is_retryable_too(self, client, db_session, video_dir, tmp_path):
        """取消 / 服务重启留下的 skipped 也要能重来 —— 它同样「没有产出」。

        重启后的任务里没轮到的条目全是 skipped（recover_interrupted_jobs 的
        error_message 里写着「可直接重新发起」），只放宽 failed 的话那句话兑不了现。
        """
        source, _ = video_dir
        job = _create_job(client, source, tmp_path / "字幕")
        _settle(
            db_session,
            job["id"],
            {2: SubtitleJobItemStatus.SKIPPED},
            job_status="cancelled",
        )

        response = client.post(f"/api/v1/subtitle/jobs/{job['id']}/items/2/retry")
        assert response.status_code == 200, response.text
        item = next(it for it in response.json()["data"]["items"] if it["index"] == 2)
        assert item["status"] == "pending"
        # 没被点的那条第 1 条保持成功
        other = next(it for it in response.json()["data"]["items"] if it["index"] == 1)
        assert other["status"] == "success"

    def test_successful_item_cannot_be_retried(self, client, db_session, video_dir, tmp_path):
        source, _ = video_dir
        job = _create_job(client, source, tmp_path / "字幕")
        _settle(db_session, job["id"], {1: SubtitleJobItemStatus.FAILED})

        response = client.post(f"/api/v1/subtitle/jobs/{job['id']}/items/2/retry")
        assert response.status_code == 409
        assert "只有失败或跳过" in response.json()["error"]["message"]

    def test_unfinished_job_cannot_be_retried(self, client, video_dir, tmp_path):
        """还在排队 / 正在跑的任务不能重试（工作线程正拿着它）。"""
        source, _ = video_dir
        job = _create_job(client, source, tmp_path / "字幕")
        response = client.post(f"/api/v1/subtitle/jobs/{job['id']}/items/1/retry")
        assert response.status_code == 409
        assert "尚未结束" in response.json()["error"]["message"]

    def test_unknown_index_is_404(self, client, db_session, video_dir, tmp_path):
        source, _ = video_dir
        job = _create_job(client, source, tmp_path / "字幕")
        _settle(db_session, job["id"], {1: SubtitleJobItemStatus.FAILED})

        response = client.post(f"/api/v1/subtitle/jobs/{job['id']}/items/9/retry")
        assert response.status_code == 404
        assert "任务条目不存在" in response.json()["error"]["message"]

    def test_unknown_job_is_404(self, client):
        assert client.post("/api/v1/subtitle/jobs/999/items/1/retry").status_code == 404

    def test_retained_subtitle_is_kept(self, client, db_session, video_dir, tmp_path):
        """重试不删上一轮已经写好的字幕。

        被取消的条目**可能确实有产物**：执行器写完 .srt 才发现被取消时，会特意
        告诉用户「任务已取消，但字幕已生成（已保留在输出目录）」
        （subtitle_runner 里 aborted == "cancelled" 那一支）。既然答应保留了，
        重试就不能反手把它删掉 —— 用户手里那份字幕会凭空消失。
        三个派生字段（subtitle_exists / file_size / segment_count）同样保留：
        条目重跑完 _collect_result 会按新文件的实际情况重新推导，不会失真。
        """
        source, _ = video_dir
        job = _create_job(client, source, tmp_path / "字幕")
        _settle(db_session, job["id"], {1: SubtitleJobItemStatus.SKIPPED})
        # 手工摆出「字幕已写好、随后被取消」的形状：_collect_result 先写
        # subtitle_exists，_skip_item 再改状态（_settle 只给成功的条目写文件）
        item = db_session.get(SubtitleJob, job["id"]).items[0]
        kept = Path(item.output_path)
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_text(DEFAULT_SRT, encoding="utf-8")
        item.subtitle_exists = True
        item.file_size = kept.stat().st_size
        item.segment_count = 2
        item.error_message = "任务已取消，但字幕已生成（已保留在输出目录）"
        db_session.commit()

        data = client.post(
            f"/api/v1/subtitle/jobs/{job['id']}/items/1/retry"
        ).json()["data"]

        assert kept.is_file(), "重试不该删掉已经保留给用户的字幕"
        retried = next(it for it in data["items"] if it["index"] == 1)
        assert retried["status"] == "pending"
        assert retried["subtitle_exists"] is True
        # 字幕还在，计数就该照旧算它一份
        assert data["subtitle_count"] == 2

    def test_counts_are_recounted(self, client, db_session, video_dir, tmp_path):
        """completed / subtitle_count 都是执行器 += 1 出来的、收尾不会重算，
        重试时必须按条目重数。

        两条里第 2 条失败：重试后 completed 应当是「已成功的 1 条」，
        而不是原来的 2（那样这条重跑完会变成 3/2）。
        """
        source, _ = video_dir
        job = _create_job(client, source, tmp_path / "字幕")
        _settle(db_session, job["id"], {2: SubtitleJobItemStatus.FAILED})
        assert job["total_videos"] == 2

        data = client.post(
            f"/api/v1/subtitle/jobs/{job['id']}/items/2/retry"
        ).json()["data"]
        assert data["completed_videos"] == 1
        assert data["failed_videos"] == 0
        assert data["skipped_videos"] == 0
        assert data["subtitle_count"] == 1


class TestRetryAll:
    """一键重试：把失败 + 跳过的条目一起重新入队，成功的原样不动。"""

    def test_every_unfinished_item_is_requeued(self, client, db_session, video_dir, tmp_path):
        source, _ = video_dir
        job = _create_job(client, source, tmp_path / "字幕")
        _settle(
            db_session,
            job["id"],
            {2: SubtitleJobItemStatus.FAILED},
            job_status="cancelled",
        )
        kept = Path(db_session.get(SubtitleJob, job["id"]).items[0].output_path)

        data = client.post(f"/api/v1/subtitle/jobs/{job['id']}/retry").json()["data"]
        assert data["status"] == "pending"
        assert [it["status"] for it in data["items"]] == ["success", "pending"]
        assert data["completed_videos"] == 1
        assert data["failed_videos"] == 0 and data["skipped_videos"] == 0
        assert kept.read_text(encoding="utf-8") == DEFAULT_SRT

    def test_second_call_is_409(self, client, db_session, video_dir, tmp_path):
        """第一次调用就把任务置回 pending，第二次必须 409 —— 这正是「批量不能
        在前端循环调单条」要防的场景（循环会在第二次 409 时半途而废）。"""
        source, _ = video_dir
        job = _create_job(client, source, tmp_path / "字幕")
        _settle(db_session, job["id"], {1: SubtitleJobItemStatus.FAILED})

        assert client.post(f"/api/v1/subtitle/jobs/{job['id']}/retry").status_code == 200
        second = client.post(f"/api/v1/subtitle/jobs/{job['id']}/retry")
        assert second.status_code == 409
        assert "尚未结束" in second.json()["error"]["message"]

    def test_nothing_to_retry_is_409(self, client, db_session, video_dir, tmp_path):
        source, _ = video_dir
        job = _create_job(client, source, tmp_path / "字幕")
        _settle(db_session, job["id"], {}, job_status="success")

        response = client.post(f"/api/v1/subtitle/jobs/{job['id']}/retry")
        assert response.status_code == 409
        assert "没有可重试的条目" in response.json()["error"]["message"]

    def test_unfinished_job_cannot_be_retried(self, client, video_dir, tmp_path):
        source, _ = video_dir
        job = _create_job(client, source, tmp_path / "字幕")
        assert client.post(f"/api/v1/subtitle/jobs/{job['id']}/retry").status_code == 409


class TestRetryWithRunner:
    """重试之后真跑一遍：只处理被重试的那些，已成功的产物不被重写。"""

    def test_only_the_retried_item_is_processed(self, db_session, tmp_path):
        FakeVcPopen.reset()
        job = _make_job(db_session, tmp_path)
        # 第一次跑：第 1 条退出码非 0 且没产出 .srt → 判失败；第 2 条正常
        _make_runner(scripts=[{"exit_code": 1, "no_output": True}, {}]).run_job(job.id)

        job = _refresh(db_session, job)
        assert job.status == SubtitleJobStatus.PARTIAL
        kept = Path(job.items[1].output_path)
        kept_text = kept.read_text(encoding="utf-8")

        SubtitleJobService(db_session).retry_item(job.id, 1)

        before = len(FakeVcPopen.instances)
        _make_runner().run_job(job.id)

        # 只起了一次子进程，而且跑的是被重试的第 1 条
        assert len(FakeVcPopen.instances) - before == 1
        argv = FakeVcPopen.instances[-1].argv
        assert argv[argv.index("transcribe") + 1] == job.items[0].source_path
        assert kept.read_text(encoding="utf-8") == kept_text   # 成功那条没被重写

        refreshed = _refresh(db_session, job)
        assert refreshed.status == SubtitleJobStatus.SUCCESS
        assert refreshed.completed_videos == 2
        assert refreshed.failed_videos == 0
        assert refreshed.subtitle_count == 2
