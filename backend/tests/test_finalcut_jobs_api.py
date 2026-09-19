"""一键成品接口测试：环境自检、AI 配置读写、文案任务的信封与状态机契约。

执行层（runner 调 AI 的全流程）在 test_finalcut_copy.py —— 这里只钉接口层：
创建校验（路径/后缀/存在性 → 422/400）、轮询、取消、删除与批量删除。

AI 配置的写接口会**真的读写 .env**：autouse 夹具把 ai_settings._ENV_PATH 指到
tmp，绝不能碰开发机上的 backend/.env（里面是用户自己的 key）。
"""

from pathlib import Path

import pytest

from app.core.config import settings
from app.services import ai_settings

#: 假探测返回的视频规格（与 mix_runner.probe_video_spec 的键形状一致）。
_FAKE_SPEC = {
    "width": 1080,
    "height": 1920,
    "duration": 15.0,
    "fps": 30.0,
    "codec": "h264",
    "audio_codec": "aac",
}

_FAKE_ENV = {
    "ready": True,
    "ai": {
        "configured": True,
        "ok": True,
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_present": True,
        "key_masked": "sk-****cdef",
        "detail": "",
        "fix_hint": "",
    },
    "ffmpeg": {
        "ok": True,
        "path": "/fake/ffmpeg",
        "version": "9.9.9",
        "has_drawtext": True,
        "supports_boxborderw": True,
        "detail": "",
        "fix_hint": "",
    },
    "font": {"file": "/fake/msyh.ttc", "family": "微软雅黑"},
    "default_output_dir": "/fake/materials/finalcut",
    "text_styles": [
        {
            "key": "white_box",
            "label": "白字黑边带底",
            "preview_text": "#FFFFFF",
            "preview_background": "#000000",
            "default": True,
        }
    ],
    "default_style": "white_box",
    "copy_count_default": 5,
    "copy_count_max": 10,
    "max_items": 10,
    "warnings": [],
}


@pytest.fixture(autouse=True)
def _isolate_ai_settings(monkeypatch, tmp_path):
    """把 AI 配置的持久化与运行态全隔离掉（与字幕设置的隔离夹具同一套理由）。

    - `.env` 指到 tmp：写接口会真的读写这个文件；
    - settings 单例做快照：接口里的赋值是裸赋值，monkeypatch 拦不住写入本身，
      只能靠 teardown 恢复；
    - 清掉同名环境变量：它会盖过 .env，让断言取决于开发机的 shell；
    - setup 时把单例的 AI_API_KEY 清空：它可能带着开发机 .env 里配好的真
      key 进进程，「未配置 key」的断言否则取决于机器状态（真机踩过）。
    """
    monkeypatch.setattr(ai_settings, "_ENV_PATH", tmp_path / ".env")
    for key in ("AI_BASE_URL", "AI_MODEL", "AI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    originals = (settings.AI_BASE_URL, settings.AI_MODEL, settings.AI_API_KEY)
    settings.AI_API_KEY = ""
    yield
    settings.AI_BASE_URL, settings.AI_MODEL, settings.AI_API_KEY = originals


@pytest.fixture(autouse=True)
def _stub_probe_video_spec(monkeypatch):
    """创建任务要 ffprobe 探时长 —— 测试不该依赖真 ffprobe 与真视频。

    service 是 `from ... import probe_video_spec` 拿的名字，桩要打在
    使用方（finalcut_copy_service）的命名空间里。
    """
    monkeypatch.setattr(
        "app.services.finalcut_copy_service.probe_video_spec",
        lambda path: dict(_FAKE_SPEC),
    )


@pytest.fixture()
def media_files(tmp_path):
    """造一对可用的素材文件：字幕 + 成片（内容是假的，探规格已打桩）。"""
    subtitle = tmp_path / "字幕.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n这个收纳架真的绝了\n",
        encoding="utf-8",
    )
    video = tmp_path / "成片.mp4"
    video.write_bytes(b"fake-video")
    return subtitle, video


def _create_job(client, media_files, **extra) -> dict:
    subtitle, video = media_files
    payload = {"subtitle_path": str(subtitle), "video_path": str(video), **extra}
    response = client.post("/api/v1/finalcut/copy-jobs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


# ---------------------------------------------------------------------------
# 环境自检
# ---------------------------------------------------------------------------


class TestEnvironment:
    def test_environment_envelope(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.api.v1.finalcut_jobs.probe_environment", lambda refresh=False: dict(_FAKE_ENV)
        )
        response = client.get("/api/v1/finalcut/environment")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        data = body["data"]
        assert data["ready"] is True
        assert data["ai"]["key_masked"] == "sk-****cdef"
        assert data["ffmpeg"]["has_drawtext"] is True
        assert data["font"]["family"] == "微软雅黑"
        assert data["text_styles"][0]["key"] == "white_box"

    def test_environment_not_ready_carries_fix_hints(self, client, monkeypatch):
        env = dict(_FAKE_ENV)
        env["ready"] = False
        env["ai"] = {**_FAKE_ENV["ai"], "configured": False, "ok": False,
                     "key_present": False, "key_masked": "",
                     "fix_hint": "点右上角「AI 配置」填写 API key"}
        monkeypatch.setattr(
            "app.api.v1.finalcut_jobs.probe_environment", lambda refresh=False: env
        )
        data = client.get("/api/v1/finalcut/environment").json()["data"]
        assert data["ready"] is False
        assert "AI 配置" in data["ai"]["fix_hint"]

    def test_refresh_syncs_env_file_into_settings(self, client, monkeypatch, tmp_path):
        """refresh=true 会把 .env 里的 AI 键同步进运行中的 settings（手改 .env 的场景）。"""
        monkeypatch.setattr(
            "app.api.v1.finalcut_jobs.probe_environment", lambda refresh=False: dict(_FAKE_ENV)
        )
        (tmp_path / ".env").write_text(
            "AI_API_KEY=sk-fromenv123456\n", encoding="utf-8"
        )
        settings.AI_API_KEY = ""  # 模拟「进程里是空的，.env 里后来被人填了」

        response = client.get("/api/v1/finalcut/environment", params={"refresh": "true"})

        assert response.status_code == 200
        assert settings.AI_API_KEY == "sk-fromenv123456"


# ---------------------------------------------------------------------------
# AI 配置读写
# ---------------------------------------------------------------------------


class TestAiSettings:
    def test_get_returns_defaults_and_no_full_key(self, client):
        response = client.get("/api/v1/finalcut/settings")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["base_url"] == settings.AI_BASE_URL
        assert data["model"] == settings.AI_MODEL
        assert data["api_key_present"] is False
        assert data["api_key_masked"] == ""
        assert "api_key" not in data  # 完整 key 字段根本不存在

    def test_get_masks_configured_key(self, client):
        settings.AI_API_KEY = "sk-abcdef123456"
        data = client.get("/api/v1/finalcut/settings").json()["data"]
        assert data["api_key_present"] is True
        assert data["api_key_masked"] == "sk-****3456"
        assert "sk-abcdef123456" not in response_text(client)

    def test_put_writes_env_and_hot_syncs(self, client, tmp_path):
        response = client.put(
            "/api/v1/finalcut/settings",
            json={
                "base_url": "https://api.deepseek.com/v1/",
                "model": "deepseek-chat",
                "api_key": "sk-newkey98765",
            },
        )
        assert response.status_code == 200, response.text

        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "AI（一键成品的文案生成）" in env_text  # 段头
        assert "AI_API_KEY=sk-newkey98765" in env_text
        # 写盘只保证下次启动生效，当前进程靠原地改单例立刻生效
        assert settings.AI_API_KEY == "sk-newkey98765"
        assert settings.AI_BASE_URL == "https://api.deepseek.com/v1"  # 尾斜杠被剥掉

        data = response.json()["data"]
        assert data["api_key_present"] is True
        assert "sk-newkey98765" not in response.text  # 响应里只有掩码

    def test_put_empty_key_keeps_existing(self, client, tmp_path):
        """只想改模型时 key 留空 = 不动它（读接口只给掩码，回填不了原值）。"""
        settings.AI_API_KEY = "sk-existing0000"
        (tmp_path / ".env").write_text(
            "AI_API_KEY=sk-existing0000\n", encoding="utf-8"
        )

        response = client.put(
            "/api/v1/finalcut/settings",
            json={"base_url": "https://example.com/v1", "model": "别的模型", "api_key": ""},
        )
        assert response.status_code == 200, response.text

        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "sk-existing0000" in env_text  # 原 key 没被抹掉
        assert settings.AI_API_KEY == "sk-existing0000"
        assert settings.AI_MODEL == "别的模型"

    def test_put_empty_base_url_returns_400(self, client):
        response = client.put(
            "/api/v1/finalcut/settings",
            json={"base_url": "   ", "model": "deepseek-chat"},
        )
        assert response.status_code == 400
        assert response.json()["success"] is False

    def test_put_unsafe_chars_returns_400(self, client, tmp_path):
        """key 里带换行这类写不进 .env 的字符 → 400，而不是 500 堆栈。"""
        response = client.put(
            "/api/v1/finalcut/settings",
            json={
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "api_key": "sk-line1\nline2",
            },
        )
        assert response.status_code == 400
        assert not (tmp_path / ".env").exists()  # 没写半个文件出去


def response_text(client) -> str:
    """读当前 settings 接口的原始响应文本（给「完整 key 不出后端」断言用）。"""
    return client.get("/api/v1/finalcut/settings").text


# ---------------------------------------------------------------------------
# 创建文案任务：输入校验
# ---------------------------------------------------------------------------


class TestCreateCopyJob:
    def test_create_success_201(self, client, media_files):
        data = _create_job(client, media_files, hint="主打性价比")
        assert data["status"] == "pending"
        assert data["video_duration"] == 15.0  # 探测桩给的时长固化进了任务
        assert data["copy_count"] == settings.FINALCUT_COPY_COUNT_DEFAULT  # 0 → 默认
        assert data["hint"] == "主打性价比"
        assert data["model"] == settings.AI_MODEL
        assert data["result"] is None
        assert data["progress_percent"] == 0.0

    def test_relative_path_422(self, client):
        response = client.post(
            "/api/v1/finalcut/copy-jobs",
            json={"subtitle_path": "相对路径/字幕.srt", "video_path": "D:/v/成片.mp4"},
        )
        assert response.status_code == 422

    def test_bad_subtitle_extension_422(self, client, media_files):
        _, video = media_files
        response = client.post(
            "/api/v1/finalcut/copy-jobs",
            json={"subtitle_path": str(video), "video_path": str(video)},
        )
        assert response.status_code == 422  # .mp4 不在字幕白名单里

    def test_bad_video_extension_422(self, client, media_files):
        subtitle, _ = media_files
        response = client.post(
            "/api/v1/finalcut/copy-jobs",
            json={"subtitle_path": str(subtitle), "video_path": str(subtitle)},
        )
        assert response.status_code == 422  # .srt 不在视频白名单里

    def test_missing_subtitle_file_400(self, client, media_files, tmp_path):
        _, video = media_files
        response = client.post(
            "/api/v1/finalcut/copy-jobs",
            json={
                "subtitle_path": str(tmp_path / "不存在.srt"),
                "video_path": str(video),
            },
        )
        assert response.status_code == 400
        assert "字幕文件不存在" in response.text

    def test_missing_video_file_400(self, client, media_files, tmp_path):
        subtitle, _ = media_files
        response = client.post(
            "/api/v1/finalcut/copy-jobs",
            json={
                "subtitle_path": str(subtitle),
                "video_path": str(tmp_path / "不存在.mp4"),
            },
        )
        assert response.status_code == 400
        assert "视频文件不存在" in response.text

    def test_unreadable_video_spec_400(self, client, media_files, monkeypatch):
        """ffprobe 探不出时长（文件损坏）→ 创建时就报清楚，不排队后才炸。"""
        monkeypatch.setattr(
            "app.services.finalcut_copy_service.probe_video_spec", lambda path: None
        )
        response = client.post(
            "/api/v1/finalcut/copy-jobs",
            json={
                "subtitle_path": str(media_files[0]),
                "video_path": str(media_files[1]),
            },
        )
        assert response.status_code == 400
        assert "读不出视频规格" in response.text

    def test_empty_subtitle_400(self, client, media_files, tmp_path):
        empty = tmp_path / "空字幕.srt"
        empty.write_text("1\n00:00:00,000 --> 00:00:02,000\n", encoding="utf-8")
        response = client.post(
            "/api/v1/finalcut/copy-jobs",
            json={"subtitle_path": str(empty), "video_path": str(media_files[1])},
        )
        assert response.status_code == 400
        assert "没有可用文本" in response.text

    def test_copy_count_over_max_422(self, client, media_files):
        response = client.post(
            "/api/v1/finalcut/copy-jobs",
            json={
                "subtitle_path": str(media_files[0]),
                "video_path": str(media_files[1]),
                "copy_count": settings.FINALCUT_COPY_COUNT_MAX + 1,
            },
        )
        assert response.status_code == 422

    def test_hint_too_long_422(self, client, media_files):
        response = client.post(
            "/api/v1/finalcut/copy-jobs",
            json={
                "subtitle_path": str(media_files[0]),
                "video_path": str(media_files[1]),
                "hint": "长" * (settings.FINALCUT_HINT_MAX_CHARS + 1),
            },
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# 轮询 / 取消 / 删除
# ---------------------------------------------------------------------------


class TestCopyJobLifecycle:
    def test_get_and_list(self, client, media_files):
        created = _create_job(client, media_files)

        detail = client.get(f"/api/v1/finalcut/copy-jobs/{created['id']}")
        assert detail.status_code == 200
        assert detail.json()["data"]["status"] == "pending"
        # 轮询界面要的字段都在
        assert "current_phase" in detail.json()["data"]
        assert "progress_percent" in detail.json()["data"]

        listing = client.get("/api/v1/finalcut/copy-jobs")
        assert listing.status_code == 200
        data = listing.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == created["id"]

    def test_list_status_filter(self, client, media_files):
        created = _create_job(client, media_files)
        client.post(f"/api/v1/finalcut/copy-jobs/{created['id']}/cancel")
        _create_job(client, media_files)

        cancelled = client.get("/api/v1/finalcut/copy-jobs", params={"status": "cancelled"})
        assert cancelled.json()["data"]["total"] == 1
        pending = client.get("/api/v1/finalcut/copy-jobs", params={"status": "pending"})
        assert pending.json()["data"]["total"] == 1

    def test_get_missing_404(self, client):
        response = client.get("/api/v1/finalcut/copy-jobs/9999")
        assert response.status_code == 404
        assert response.json()["success"] is False

    def test_job_id_zero_422(self, client):
        """路径参数 ge=1：0 与负数直接 422，不进业务层。"""
        assert client.get("/api/v1/finalcut/copy-jobs/0").status_code == 422

    def test_cancel_pending(self, client, media_files):
        created = _create_job(client, media_files)
        response = client.post(f"/api/v1/finalcut/copy-jobs/{created['id']}/cancel")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "cancelled"
        assert data["finished_at"] is not None

    def test_cancel_terminal_conflict_409(self, client, media_files):
        created = _create_job(client, media_files)
        client.post(f"/api/v1/finalcut/copy-jobs/{created['id']}/cancel")
        again = client.post(f"/api/v1/finalcut/copy-jobs/{created['id']}/cancel")
        assert again.status_code == 409

    def test_cancel_missing_404(self, client):
        assert client.post("/api/v1/finalcut/copy-jobs/9999/cancel").status_code == 404

    def test_delete_terminal(self, client, media_files):
        created = _create_job(client, media_files)
        client.post(f"/api/v1/finalcut/copy-jobs/{created['id']}/cancel")
        response = client.delete(f"/api/v1/finalcut/copy-jobs/{created['id']}")
        assert response.status_code == 200
        assert client.get(f"/api/v1/finalcut/copy-jobs/{created['id']}").status_code == 404

    def test_delete_pending_conflict_409(self, client, media_files):
        created = _create_job(client, media_files)
        response = client.delete(f"/api/v1/finalcut/copy-jobs/{created['id']}")
        assert response.status_code == 409
        # 没删掉
        assert client.get(f"/api/v1/finalcut/copy-jobs/{created['id']}").status_code == 200

    def test_delete_missing_404(self, client):
        assert client.delete("/api/v1/finalcut/copy-jobs/9999").status_code == 404

    def test_batch_delete(self, client, media_files):
        first = _create_job(client, media_files)
        second = _create_job(client, media_files)
        for job in (first, second):
            client.post(f"/api/v1/finalcut/copy-jobs/{job['id']}/cancel")

        response = client.post(
            "/api/v1/finalcut/copy-jobs/batch-delete",
            json={"ids": [first["id"], second["id"]]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["count"] == 2
        assert client.get("/api/v1/finalcut/copy-jobs").json()["data"]["total"] == 0

    def test_batch_delete_rolls_back_on_pending(self, client, media_files):
        """整批回滚：其中一个还在 pending，就一个都不删。"""
        done = _create_job(client, media_files)
        pending = _create_job(client, media_files)
        client.post(f"/api/v1/finalcut/copy-jobs/{done['id']}/cancel")

        response = client.post(
            "/api/v1/finalcut/copy-jobs/batch-delete",
            json={"ids": [done["id"], pending["id"]]},
        )
        assert response.status_code == 409
        # 一个都没删
        assert client.get("/api/v1/finalcut/copy-jobs").json()["data"]["total"] == 2

    def test_batch_delete_missing_id_404(self, client):
        """静态路由 /copy-jobs/batch-delete 不能被 /{job_id} 吞掉（注册顺序）。"""
        response = client.post(
            "/api/v1/finalcut/copy-jobs/batch-delete", json={"ids": [9999]}
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# 合成任务（render-jobs）
# ---------------------------------------------------------------------------

from app.services.finalcut_env import FontChoice  # noqa: E402

#: render 服务要的是完整规格（宽高/fps/音轨），键形状与 mix_runner.probe_video_spec 一致
_FAKE_RENDER_SPEC = {
    "width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1,
    "rotation": 0, "has_audio": True, "duration": 15.0, "audio_codec": "aac",
}

#: 一条合法的合成条目（框选 + 样式 + 自动字号）
_ITEM = {
    "copy_text": "这条文案会烧进画面",
    "angle": "痛点开场",
    "style": "white_box",
    "box": {"x": 0.1, "y": 0.7, "w": 0.8, "h": 0.2},
    "font_size": 0,
}


@pytest.fixture(autouse=True)
def _isolate_materials(monkeypatch, tmp_path):
    """素材根指到 tmp：默认产物目录会真的 mkdir，绝不能写到开发机的 materials/。"""
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(tmp_path / "materials"))


@pytest.fixture(autouse=True)
def _stub_render_deps(monkeypatch, tmp_path):
    """render 创建校验的外部依赖：ffprobe 规格探测 + 中文字体探测。

    桩打在使用方（finalcut_render_service）的命名空间上；字体文件要真存在
    （runner 复核时只认 is_file）。
    """
    font = tmp_path / "test-font.ttf"
    font.write_bytes(b"fake-font-bytes")
    monkeypatch.setattr(
        "app.services.finalcut_render_service.probe_video_spec",
        lambda path: dict(_FAKE_RENDER_SPEC),
    )
    monkeypatch.setattr(
        "app.services.finalcut_render_service.detect_font",
        lambda: FontChoice(file=str(font), family="测试字体"),
    )
    return font


def _create_render_job(client, media_files, items=None, **extra) -> dict:
    _, video = media_files
    payload = {
        "video_path": str(video),
        "items": items if items is not None else [dict(_ITEM)],
        **extra,
    }
    response = client.post("/api/v1/finalcut/render-jobs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


class TestCreateRenderJob:
    def test_create_success_201(self, client, media_files):
        items = [dict(_ITEM), {**_ITEM, "copy_text": "第二条", "style": "yellow"}]
        data = _create_render_job(client, media_files, items=items)

        assert data["status"] == "pending"
        assert data["total_items"] == 2
        assert data["video_duration"] == 15.0
        assert data["video_spec"]["width"] == 1080  # 探测桩的规格快照落库
        assert data["video_spec"]["audio_codec"] == "aac"
        assert data["font_file"]  # 字体快照非空
        assert "finalcut-" in data["output_dir"]
        assert Path(data["output_dir"]).is_dir()  # 输出目录创建时就建好

        assert [item["index"] for item in data["items"]] == [1, 2]
        first = data["items"][0]
        assert first["copy_text"] == _ITEM["copy_text"]
        assert first["box"] == _ITEM["box"]
        assert first["status"] == "pending"
        # 没产出前不给媒体地址
        assert first["video_url"] == "" and first["thumb_url"] == ""

    def test_create_with_copy_job_id(self, client, media_files):
        copy_job = _create_job(client, media_files)
        data = _create_render_job(client, media_files, copy_job_id=copy_job["id"])
        assert data["copy_job_id"] == copy_job["id"]

    def test_missing_copy_job_400(self, client, media_files):
        _, video = media_files
        response = client.post(
            "/api/v1/finalcut/render-jobs",
            json={"video_path": str(video), "items": [dict(_ITEM)], "copy_job_id": 9999},
        )
        assert response.status_code == 400
        assert "来源文案任务不存在" in response.text

    def test_missing_video_400(self, client, tmp_path):
        response = client.post(
            "/api/v1/finalcut/render-jobs",
            json={"video_path": str(tmp_path / "不存在.mp4"), "items": [dict(_ITEM)]},
        )
        assert response.status_code == 400
        assert "视频文件不存在" in response.text

    def test_unreadable_spec_400(self, client, media_files, monkeypatch):
        monkeypatch.setattr(
            "app.services.finalcut_render_service.probe_video_spec", lambda path: None
        )
        _, video = media_files
        response = client.post(
            "/api/v1/finalcut/render-jobs",
            json={"video_path": str(video), "items": [dict(_ITEM)]},
        )
        assert response.status_code == 400
        assert "读不出视频规格" in response.text

    def test_no_font_400(self, client, media_files, monkeypatch):
        """没有中文字体是环境类问题，创建时就拦住（烧出来只会是方块）。"""
        monkeypatch.setattr(
            "app.services.finalcut_render_service.detect_font", lambda: None
        )
        _, video = media_files
        response = client.post(
            "/api/v1/finalcut/render-jobs",
            json={"video_path": str(video), "items": [dict(_ITEM)]},
        )
        assert response.status_code == 400
        assert "中文字体" in response.text

    def test_relative_path_422(self, client):
        response = client.post(
            "/api/v1/finalcut/render-jobs",
            json={"video_path": "相对/成片.mp4", "items": [dict(_ITEM)]},
        )
        assert response.status_code == 422

    def test_bad_extension_422(self, client, media_files):
        subtitle, _ = media_files
        response = client.post(
            "/api/v1/finalcut/render-jobs",
            json={"video_path": str(subtitle), "items": [dict(_ITEM)]},
        )
        assert response.status_code == 422

    def test_empty_items_422(self, client, media_files):
        _, video = media_files
        response = client.post(
            "/api/v1/finalcut/render-jobs",
            json={"video_path": str(video), "items": []},
        )
        assert response.status_code == 422

    def test_too_many_items_422(self, client, media_files):
        _, video = media_files
        items = [dict(_ITEM) for _ in range(settings.FINALCUT_MAX_ITEMS + 1)]
        response = client.post(
            "/api/v1/finalcut/render-jobs",
            json={"video_path": str(video), "items": items},
        )
        assert response.status_code == 422

    def test_unknown_style_422(self, client, media_files):
        _, video = media_files
        response = client.post(
            "/api/v1/finalcut/render-jobs",
            json={"video_path": str(video), "items": [{**_ITEM, "style": "霓虹粉"}]},
        )
        assert response.status_code == 422

    def test_box_overflow_422(self, client, media_files):
        """x + w > 1：框超出画面右边缘，schema 层就拒掉。"""
        _, video = media_files
        bad = {**_ITEM, "box": {"x": 0.5, "y": 0.1, "w": 0.6, "h": 0.2}}
        response = client.post(
            "/api/v1/finalcut/render-jobs",
            json={"video_path": str(video), "items": [bad]},
        )
        assert response.status_code == 422

    def test_blank_copy_text_422(self, client, media_files):
        _, video = media_files
        response = client.post(
            "/api/v1/finalcut/render-jobs",
            json={"video_path": str(video), "items": [{**_ITEM, "copy_text": "   "}]},
        )
        assert response.status_code == 422

    def test_missing_output_dir_400(self, client, media_files, tmp_path):
        _, video = media_files
        response = client.post(
            "/api/v1/finalcut/render-jobs",
            json={
                "video_path": str(video),
                "items": [dict(_ITEM)],
                "output_dir": str(tmp_path / "不存在的目录"),
            },
        )
        assert response.status_code == 400
        assert "输出目录" in response.text

    def test_custom_output_dir(self, client, media_files, tmp_path):
        custom = tmp_path / "我的成片"
        custom.mkdir()
        data = _create_render_job(client, media_files, output_dir=str(custom))
        assert Path(data["output_dir"]).parent == custom


class TestRenderJobLifecycle:
    def test_get_detail_with_items(self, client, media_files):
        created = _create_render_job(client, media_files)
        detail = client.get(f"/api/v1/finalcut/render-jobs/{created['id']}")
        assert detail.status_code == 200
        data = detail.json()["data"]
        assert len(data["items"]) == 1
        assert data["items"][0]["copy_text"] == _ITEM["copy_text"]

    def test_list_omits_items(self, client, media_files):
        created = _create_render_job(client, media_files)
        listing = client.get("/api/v1/finalcut/render-jobs")
        data = listing.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == created["id"]
        assert data["items"][0]["items"] == []  # 列表不携带明细

    def test_list_status_filter(self, client, media_files):
        created = _create_render_job(client, media_files)
        client.post(f"/api/v1/finalcut/render-jobs/{created['id']}/cancel")
        _create_render_job(client, media_files)
        cancelled = client.get(
            "/api/v1/finalcut/render-jobs", params={"status": "cancelled"}
        )
        assert cancelled.json()["data"]["total"] == 1

    def test_get_missing_404(self, client):
        assert client.get("/api/v1/finalcut/render-jobs/9999").status_code == 404

    def test_cancel_pending(self, client, media_files):
        created = _create_render_job(client, media_files)
        response = client.post(f"/api/v1/finalcut/render-jobs/{created['id']}/cancel")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "cancelled"
        assert data["finished_at"] is not None

    def test_cancel_terminal_conflict_409(self, client, media_files):
        created = _create_render_job(client, media_files)
        client.post(f"/api/v1/finalcut/render-jobs/{created['id']}/cancel")
        assert (
            client.post(f"/api/v1/finalcut/render-jobs/{created['id']}/cancel").status_code
            == 409
        )

    def test_delete_pending_conflict_409(self, client, media_files):
        created = _create_render_job(client, media_files)
        response = client.delete(f"/api/v1/finalcut/render-jobs/{created['id']}")
        assert response.status_code == 409

    def test_delete_keeps_files_by_default(self, client, media_files):
        """不勾 purge：只删记录，输出目录原样留在磁盘上。"""
        created = _create_render_job(client, media_files)
        out_dir = Path(created["output_dir"])
        (out_dir / "01.mp4").write_bytes(b"keep-me")
        client.post(f"/api/v1/finalcut/render-jobs/{created['id']}/cancel")

        response = client.delete(f"/api/v1/finalcut/render-jobs/{created['id']}")
        assert response.status_code == 200
        assert client.get(f"/api/v1/finalcut/render-jobs/{created['id']}").status_code == 404
        assert (out_dir / "01.mp4").read_bytes() == b"keep-me"

    def test_delete_with_purge_removes_output_dir(self, client, media_files):
        """勾了 purge：输出目录整棵清掉（与混剪同一套规矩）。"""
        created = _create_render_job(client, media_files)
        out_dir = Path(created["output_dir"])
        (out_dir / "01.mp4").write_bytes(b"purge-me")
        client.post(f"/api/v1/finalcut/render-jobs/{created['id']}/cancel")

        response = client.delete(
            f"/api/v1/finalcut/render-jobs/{created['id']}",
            params={"purge_files": "true"},
        )
        assert response.status_code == 200
        assert not out_dir.exists()

    def test_batch_delete(self, client, media_files):
        first = _create_render_job(client, media_files)
        second = _create_render_job(client, media_files)
        for job in (first, second):
            client.post(f"/api/v1/finalcut/render-jobs/{job['id']}/cancel")

        response = client.post(
            "/api/v1/finalcut/render-jobs/batch-delete",
            json={"ids": [first["id"], second["id"]], "purge_files": True},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["count"] == 2
        assert not Path(first["output_dir"]).exists()
        assert not Path(second["output_dir"]).exists()

    def test_batch_delete_rolls_back_on_pending(self, client, media_files):
        done = _create_render_job(client, media_files)
        pending = _create_render_job(client, media_files)
        client.post(f"/api/v1/finalcut/render-jobs/{done['id']}/cancel")

        response = client.post(
            "/api/v1/finalcut/render-jobs/batch-delete",
            json={"ids": [done["id"], pending["id"]]},
        )
        assert response.status_code == 409
        assert client.get("/api/v1/finalcut/render-jobs").json()["data"]["total"] == 2


class TestRenderOutputs:
    """成片视频流 / 封面：路径只从任务记录推导（item 序号），不接受外部路径。"""

    def _make_success_output(self, client, db_session, media_files):
        """把一个 render 任务改成「第 1 条已成功」并放上真的成片文件。"""
        from app.models.finalcut_job import FinalcutRenderItem

        created = _create_render_job(client, media_files)
        out_dir = Path(created["output_dir"])
        output = out_dir / "01.mp4"
        output.write_bytes(b"fake-rendered-video")
        db_session.query(FinalcutRenderItem).filter_by(job_id=created["id"]).update(
            {
                "status": "success",
                "output_path": str(output),
                "output_name": "01.mp4",
                "size_bytes": output.stat().st_size,
            }
        )
        db_session.commit()
        return created

    def test_video_full_download(self, client, db_session, media_files):
        created = self._make_success_output(client, db_session, media_files)
        response = client.get(
            f"/api/v1/finalcut/render-jobs/{created['id']}/outputs/1/video"
        )
        assert response.status_code == 200
        assert response.content == b"fake-rendered-video"
        assert response.headers["Accept-Ranges"] == "bytes"

    def test_video_range_request_206(self, client, db_session, media_files):
        created = self._make_success_output(client, db_session, media_files)
        response = client.get(
            f"/api/v1/finalcut/render-jobs/{created['id']}/outputs/1/video",
            headers={"Range": "bytes=0-3"},
        )
        assert response.status_code == 206
        assert response.content == b"fake"

    def test_detail_exposes_media_urls_after_success(
        self, client, db_session, media_files
    ):
        created = self._make_success_output(client, db_session, media_files)
        item = client.get(
            f"/api/v1/finalcut/render-jobs/{created['id']}"
        ).json()["data"]["items"][0]
        assert item["video_url"].endswith(f"/render-jobs/{created['id']}/outputs/1/video")
        assert item["thumb_url"].endswith(f"/render-jobs/{created['id']}/outputs/1/thumb")

    def test_output_index_out_of_range_404(self, client, db_session, media_files):
        created = self._make_success_output(client, db_session, media_files)
        response = client.get(
            f"/api/v1/finalcut/render-jobs/{created['id']}/outputs/99/video"
        )
        assert response.status_code == 404

    def test_pending_item_has_no_video_404(self, client, media_files):
        created = _create_render_job(client, media_files)
        response = client.get(
            f"/api/v1/finalcut/render-jobs/{created['id']}/outputs/1/video"
        )
        assert response.status_code == 404
        assert "尚未产出" in response.text

    def test_thumb_generated_on_first_access(
        self, client, db_session, media_files, monkeypatch
    ):
        def fake_thumb(src, dst) -> bool:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"\xff\xd8fake-jpeg")
            return True

        monkeypatch.setattr(
            "app.api.v1.finalcut_jobs.generate_thumbnail", fake_thumb
        )
        created = self._make_success_output(client, db_session, media_files)
        response = client.get(
            f"/api/v1/finalcut/render-jobs/{created['id']}/outputs/1/thumb"
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"

    def test_thumb_failure_404(self, client, db_session, media_files, monkeypatch):
        monkeypatch.setattr(
            "app.api.v1.finalcut_jobs.generate_thumbnail", lambda src, dst: False
        )
        created = self._make_success_output(client, db_session, media_files)
        response = client.get(
            f"/api/v1/finalcut/render-jobs/{created['id']}/outputs/1/thumb"
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# 历史产物来源与本地视频预览
# ---------------------------------------------------------------------------


class TestSources:
    def test_sources_lists_history_outputs(self, client, db_session, tmp_path):
        from app.models.mix_job import MixJob, MixJobItem, MixOutputStatus
        from app.models.subtitle_job import (
            SubtitleJob,
            SubtitleJobItem,
            SubtitleJobItemStatus,
        )

        # 一条混剪成片（文件真实存在）+ 一条已被删的（不该出现）
        video = tmp_path / "混剪成片.mp4"
        video.write_bytes(b"mix-output")
        mix = MixJob(
            status="success", opening=[], middle=[], ending=[], count=1,
            output_dir=str(tmp_path), seed=1, target={}, total_outputs=1,
        )
        db_session.add(mix)
        db_session.flush()
        db_session.add(
            MixJobItem(
                job_id=mix.id, index=1, order=("a",),
                status=MixOutputStatus.SUCCESS, output_path=str(video),
                output_name="01.mp4", duration_seconds=12.5, size_bytes=10,
            )
        )
        db_session.add(
            MixJobItem(
                job_id=mix.id, index=2, order=("b",),
                status=MixOutputStatus.SUCCESS,
                output_path=str(tmp_path / "已被删.mp4"), output_name="02.mp4",
            )
        )

        # 一条字幕产物
        srt = tmp_path / "字幕.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
        sub = SubtitleJob(
            status="success", input_path=str(tmp_path), output_dir=str(tmp_path),
        )
        db_session.add(sub)
        db_session.flush()
        db_session.add(
            SubtitleJobItem(
                job_id=sub.id, index=1, source_path=str(video), source_name="成片.mp4",
                output_path=str(srt), status=SubtitleJobItemStatus.SUCCESS,
                subtitle_exists=True, file_size=20, duration_seconds=12.5,
            )
        )
        db_session.commit()

        data = client.get("/api/v1/finalcut/sources").json()["data"]
        assert len(data["videos"]) == 1  # 被删的那条被过滤掉了
        entry = data["videos"][0]
        assert entry["path"] == str(video)
        assert entry["origin"] == "mix"
        assert entry["video_url"].endswith(f"/mix/jobs/{mix.id}/outputs/1/video")
        assert len(data["subtitles"]) == 1
        assert data["subtitles"][0]["origin"] == "subtitle"
        assert data["subtitles"][0]["video_url"] == ""

    def test_sources_empty(self, client):
        data = client.get("/api/v1/finalcut/sources").json()["data"]
        assert data == {"subtitles": [], "videos": []}


class TestPreview:
    def test_preview_ok(self, client, media_files):
        _, video = media_files
        response = client.get("/api/v1/finalcut/preview", params={"path": str(video)})
        assert response.status_code == 200
        assert response.content == b"fake-video"
        assert response.headers["Accept-Ranges"] == "bytes"

    def test_preview_range_206(self, client, media_files):
        _, video = media_files
        response = client.get(
            "/api/v1/finalcut/preview",
            params={"path": str(video)},
            headers={"Range": "bytes=0-3"},
        )
        assert response.status_code == 206
        assert response.content == b"fake"

    def test_preview_relative_path_400(self, client):
        response = client.get("/api/v1/finalcut/preview", params={"path": "a/b.mp4"})
        assert response.status_code == 400

    def test_preview_bad_extension_400(self, client, media_files):
        subtitle, _ = media_files
        response = client.get(
            "/api/v1/finalcut/preview", params={"path": str(subtitle)}
        )
        assert response.status_code == 400

    def test_preview_missing_file_404(self, client, tmp_path):
        response = client.get(
            "/api/v1/finalcut/preview", params={"path": str(tmp_path / "没有.mp4")}
        )
        assert response.status_code == 404
