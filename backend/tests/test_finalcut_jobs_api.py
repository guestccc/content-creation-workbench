"""一键成品接口测试：环境自检、AI 配置与口播语速读写、文案任务的信封与状态机契约。

执行层（runner 调 AI 的全流程）在 test_finalcut_copy.py —— 这里只钉接口层：
创建校验（路径/后缀/存在性 → 422/400）、轮询、取消、删除与批量删除。

AI 配置与语速的写接口会**真的读写 .env**：autouse 夹具把 ai_settings 与
finalcut_settings 的 _ENV_PATH 都指到 tmp，绝不能碰开发机上的 backend/.env
（里面是用户自己的 key）。
"""

from pathlib import Path

import pytest

from app.core.config import default_chars_per_second, settings
from app.schemas.common import MAX_JOB_REMARK_LENGTH
from app.services import ai_settings, finalcut_settings

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
    "chars_per_second": 5.8,
    "warnings": [],
}


@pytest.fixture(autouse=True)
def _isolate_ai_settings(monkeypatch, tmp_path):
    """把 AI 配置与口播语速的持久化与运行态全隔离掉（与字幕设置的隔离夹具同一套理由）。

    - `.env` 指到 tmp：两个写接口会真的读写这个文件（两个模块各有一个
      `_ENV_PATH`，都要指过去）；
    - settings 单例做快照：接口里的赋值是裸赋值，monkeypatch 拦不住写入本身，
      只能靠 teardown 恢复；
    - 清掉同名环境变量：它会盖过 .env，让断言取决于开发机的 shell；
    - setup 时把单例的 AI_API_KEY 清空、语速复位成默认值：它们可能带着开发机
      .env 里配好的值进进程，否则断言取决于机器状态（真机踩过）。
    """
    monkeypatch.setattr(ai_settings, "_ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(finalcut_settings, "_ENV_PATH", tmp_path / ".env")
    for key in ("AI_BASE_URL", "AI_MODEL", "AI_API_KEY", "FINALCUT_CHARS_PER_SECOND"):
        monkeypatch.delenv(key, raising=False)
    originals = (
        settings.AI_BASE_URL,
        settings.AI_MODEL,
        settings.AI_API_KEY,
        settings.FINALCUT_CHARS_PER_SECOND,
    )
    settings.AI_API_KEY = ""
    settings.FINALCUT_CHARS_PER_SECOND = default_chars_per_second()
    yield
    (
        settings.AI_BASE_URL,
        settings.AI_MODEL,
        settings.AI_API_KEY,
        settings.FINALCUT_CHARS_PER_SECOND,
    ) = originals


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
        # 语速：页面算「约念几秒」与字数预算的依据，必须随环境一起给
        assert data["chars_per_second"] == 5.8

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

    def test_refresh_syncs_rate_into_settings(self, client, monkeypatch, tmp_path):
        """手改 .env 里的语速后点「重新检测」：热同步要捡起来，且必须是 float。

        同步发生在探测**之前** —— 探测结果里的 chars_per_second 就是新值。
        """
        monkeypatch.setattr(
            "app.api.v1.finalcut_jobs.probe_environment", lambda refresh=False: dict(_FAKE_ENV)
        )
        (tmp_path / ".env").write_text(
            "FINALCUT_CHARS_PER_SECOND=6.2\n", encoding="utf-8"
        )

        response = client.get("/api/v1/finalcut/environment", params={"refresh": "true"})

        assert response.status_code == 200
        assert settings.FINALCUT_CHARS_PER_SECOND == 6.2
        # 字符串直接 setattr 会让「时长 × 语速」在任务跑起来才炸
        assert isinstance(settings.FINALCUT_CHARS_PER_SECOND, float)


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
        # 语速跟 AI 三键同一个读取接口：页面打开弹窗时一次拿全
        assert data["chars_per_second"] == default_chars_per_second()
        assert data["chars_per_second_default"] == default_chars_per_second()
        assert data["chars_per_second_warning"] == ""

    def test_get_returns_calibrated_rate(self, client):
        """用户校准过的语速要如实回填（弹窗打开时回填的就是这个值）。"""
        settings.FINALCUT_CHARS_PER_SECOND = 5.8
        data = client.get("/api/v1/finalcut/settings").json()["data"]
        assert data["chars_per_second"] == 5.8

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
                "chars_per_second": 5.8,
            },
        )
        assert response.status_code == 200, response.text

        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "AI（一键成品的文案生成）" in env_text  # 段头
        assert "AI_API_KEY=sk-newkey98765" in env_text
        assert "# ---------- 一键成品（口播语速） ----------" in env_text  # 自己一段
        assert "FINALCUT_CHARS_PER_SECOND=5.8" in env_text
        # 段头判定是子串匹配，两个段头不能互相命中：语速键必须在自己的段头之后
        lines = env_text.splitlines()
        assert lines.index("# ---------- 一键成品（口播语速） ----------") < lines.index(
            "FINALCUT_CHARS_PER_SECOND=5.8"
        )
        # 写盘只保证下次启动生效，当前进程靠原地改单例立刻生效
        assert settings.AI_API_KEY == "sk-newkey98765"
        assert settings.AI_BASE_URL == "https://api.deepseek.com/v1"  # 尾斜杠被剥掉
        assert settings.FINALCUT_CHARS_PER_SECOND == 5.8
        assert isinstance(settings.FINALCUT_CHARS_PER_SECOND, float)

        data = response.json()["data"]
        assert data["api_key_present"] is True
        assert "sk-newkey98765" not in response.text  # 响应里只有掩码
        assert data["chars_per_second"] == 5.8

    def test_put_out_of_range_rate_returns_400_and_changes_nothing(
        self, client, tmp_path
    ):
        """越界 → 400，**且一个字都没改**。

        语速先校验、再写两处：两次写是两次独立的原子写（.env 是纯文本，不做跨段
        事务），把校验前置才能兑现「报错 = 没改」这条承诺。
        """
        settings.AI_API_KEY = "sk-existing0000"
        settings.AI_MODEL = "deepseek-chat"

        response = client.put(
            "/api/v1/finalcut/settings",
            json={
                "base_url": "https://api.deepseek.com/v1",
                "model": "改过的模型",
                "api_key": "sk-newkey98765",
                "chars_per_second": 99,
            },
        )

        assert response.status_code == 400
        assert "语速" in response.text
        assert not (tmp_path / ".env").exists()  # 连 AI 那段也没写出去
        assert settings.AI_MODEL == "deepseek-chat"
        assert settings.AI_API_KEY == "sk-existing0000"
        assert settings.FINALCUT_CHARS_PER_SECOND == default_chars_per_second()

    def test_put_without_rate_keeps_existing(self, client, tmp_path):
        """不带语速 = 不改（与 api_key 留空同一套语义）：老调用方照常能保存。"""
        settings.FINALCUT_CHARS_PER_SECOND = 5.8
        (tmp_path / ".env").write_text(
            "FINALCUT_CHARS_PER_SECOND=5.8\n", encoding="utf-8"
        )

        response = client.put(
            "/api/v1/finalcut/settings",
            json={"base_url": "https://example.com/v1", "model": "别的模型"},
        )
        assert response.status_code == 200, response.text

        assert response.json()["data"]["chars_per_second"] == 5.8
        assert settings.FINALCUT_CHARS_PER_SECOND == 5.8
        assert "FINALCUT_CHARS_PER_SECOND=5.8" in (tmp_path / ".env").read_text(
            encoding="utf-8"
        )

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

    def test_rate_from_request_is_snapshotted(self, client, media_files):
        """请求里带的语速进任务记录 —— 「这条念快些」就是靠它。"""
        created = _create_job(client, media_files, chars_per_second=5.8)
        assert created["chars_per_second"] == 5.8

        # 详情接口读的是同一个快照（列表/轮询都靠它展示秒数）
        detail = client.get(f"/api/v1/finalcut/copy-jobs/{created['id']}").json()["data"]
        assert detail["chars_per_second"] == 5.8

    def test_rate_defaults_to_current_global(self, client, media_files):
        """不带语速 = 用当前全局默认值，并且**当场固化**，之后改全局不影响它。"""
        created = _create_job(client, media_files)
        assert created["chars_per_second"] == default_chars_per_second()

        settings.FINALCUT_CHARS_PER_SECOND = 4.0
        later = _create_job(client, media_files)
        assert later["chars_per_second"] == 4.0  # 新任务跟新默认值

        # 已建的那条不受影响：历史任务的预算必须与当时生成的内容对得上
        detail = client.get(f"/api/v1/finalcut/copy-jobs/{created['id']}").json()["data"]
        assert detail["chars_per_second"] == default_chars_per_second()

    def test_rate_out_of_range_422(self, client, media_files):
        """区间与配置弹窗同一套（1.0–15.0），越界在创建时就被挡下。"""
        for bad in (0.5, 99):
            response = client.post(
                "/api/v1/finalcut/copy-jobs",
                json={
                    "subtitle_path": str(media_files[0]),
                    "video_path": str(media_files[1]),
                    "chars_per_second": bad,
                },
            )
            assert response.status_code == 422, response.text

    def test_rate_not_a_number_422(self, client, media_files):
        response = client.post(
            "/api/v1/finalcut/copy-jobs",
            json={
                "subtitle_path": str(media_files[0]),
                "video_path": str(media_files[1]),
                "chars_per_second": "很快",
            },
        )
        assert response.status_code == 422

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

    def test_detail_opens_legacy_result_with_target_seconds(self, client, db_session, tmp_path):
        """历史任务的 result 里残留 target_seconds（已删字段）→ 详情照常返回。

        页面上「点开一条老任务」走的就是这条接口。CopyCandidate 没开
        extra='forbid'，pydantic v2 默认忽略未知键 —— 老任务不会因为字段退场
        而打不开，也不需要数据库迁移。
        """
        from app.models.finalcut_job import FinalcutCopyJob

        job = FinalcutCopyJob(
            status="success",
            subtitle_path=str(tmp_path / "字幕.srt"),
            video_path=str(tmp_path / "成片.mp4"),
            video_duration=36.294, copy_count=5, hint="", model="deepseek-chat",
            result={
                "analysis": {"topic": "厨房收纳"},
                "copies": [
                    {
                        "text": "老文案",
                        "angle": "痛点开场",
                        "target_seconds": 36.3,
                        "char_count": 87,
                        "why": "",
                        "highlights": [],
                        "breakdown": [],
                    }
                ],
            },
        )
        db_session.add(job)
        db_session.commit()

        response = client.get(f"/api/v1/finalcut/copy-jobs/{job.id}")
        assert response.status_code == 200, response.text
        copy = response.json()["data"]["result"]["copies"][0]
        assert copy["text"] == "老文案"
        assert copy["char_count"] == 87
        assert "target_seconds" not in copy

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
# 文案任务备注
# ---------------------------------------------------------------------------


class TestCopyJobRetry:
    """重试：按旧任务的参数**新建一条任务**（POST /copy-jobs/{id}/retry）。

    文案任务没有条目级状态（一次 AI 调用产出一批文案），所以只能整任务重跑；
    而重跑必须是新建 —— 一条任务的产物就是「那一批文案」，就地重跑会把上一批
    覆盖掉，历史记录与产物再也对不上。
    """

    def test_retry_creates_a_new_job(self, client, media_files):
        """返回 201 + 一条全新的 pending 任务，素材与参数原样带过去。"""
        created = _create_job(client, media_files, copy_count=3, hint="走痛点")

        response = client.post(f"/api/v1/finalcut/copy-jobs/{created['id']}/retry")
        assert response.status_code == 201, response.text
        new = response.json()["data"]

        assert new["id"] != created["id"]
        assert new["status"] == "pending"
        assert new["subtitle_path"] == created["subtitle_path"]
        assert new["video_path"] == created["video_path"]
        assert new["video_duration"] == created["video_duration"]
        assert new["copy_count"] == 3
        assert new["hint"] == "走痛点"
        assert new["result"] is None and new["error_message"] == ""

    def test_old_job_is_not_touched(self, client, media_files):
        """旧任务原样留档：用户要靠它对比两批文案。"""
        created = _create_job(client, media_files)
        client.post(f"/api/v1/finalcut/copy-jobs/{created['id']}/cancel")

        client.post(f"/api/v1/finalcut/copy-jobs/{created['id']}/retry")

        detail = client.get(f"/api/v1/finalcut/copy-jobs/{created['id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["status"] == "cancelled"
        assert client.get("/api/v1/finalcut/copy-jobs").json()["data"]["total"] == 2

    def test_rate_is_taken_from_the_snapshot_not_todays_global(self, client, media_files):
        """重试照抄当时快照的语速，而不是今天的全局默认值。"""
        created = _create_job(client, media_files, chars_per_second=5.8)
        settings.FINALCUT_CHARS_PER_SECOND = 4.0

        new = client.post(
            f"/api/v1/finalcut/copy-jobs/{created['id']}/retry"
        ).json()["data"]

        assert new["chars_per_second"] == 5.8

    def test_missing_subtitle_fails_fast_400(self, client, media_files, tmp_path):
        """素材已删时点击即 400，且不留下半条任务记录。"""
        created = _create_job(client, media_files)
        (tmp_path / "字幕.srt").unlink()

        response = client.post(f"/api/v1/finalcut/copy-jobs/{created['id']}/retry")
        assert response.status_code == 400, response.text
        assert "字幕文件不存在" in response.json()["error"]["message"]
        assert client.get("/api/v1/finalcut/copy-jobs").json()["data"]["total"] == 1

    def test_unknown_job_is_404(self, client):
        assert client.post("/api/v1/finalcut/copy-jobs/9999/retry").status_code == 404


class TestCopyJobRemark:
    """备注是给用户自己看的标记：写过之后详情与列表都要回显。"""

    def test_save_echoes_in_detail_and_list(self, client, media_files):
        created = _create_job(client, media_files)
        response = client.put(
            f"/api/v1/finalcut/copy-jobs/{created['id']}/remark",
            json={"remark": "只投 A 组"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["remark"] == "只投 A 组"

        detail = client.get(f"/api/v1/finalcut/copy-jobs/{created['id']}").json()["data"]
        assert detail["remark"] == "只投 A 组"
        items = client.get("/api/v1/finalcut/copy-jobs").json()["data"]["items"]
        assert items[0]["remark"] == "只投 A 组"

    def test_empty_string_clears_remark(self, client, media_files):
        """空串是有效值（清空），不是「本次不改」—— 与 AI 配置留空即不改的语义相反。"""
        created = _create_job(client, media_files)
        path = f"/api/v1/finalcut/copy-jobs/{created['id']}/remark"
        client.put(path, json={"remark": "先写一条"})

        response = client.put(path, json={"remark": ""})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["remark"] == ""
        assert client.get(path.rsplit("/remark", 1)[0]).json()["data"]["remark"] == ""

    def test_remark_too_long_422(self, client, media_files):
        created = _create_job(client, media_files)
        response = client.put(
            f"/api/v1/finalcut/copy-jobs/{created['id']}/remark",
            json={"remark": "长" * (MAX_JOB_REMARK_LENGTH + 1)},
        )
        assert response.status_code == 422
        # 超长整条被拒，没落半个字进去
        detail = client.get(f"/api/v1/finalcut/copy-jobs/{created['id']}").json()["data"]
        assert detail["remark"] == ""

    def test_remark_missing_job_404(self, client):
        response = client.put(
            "/api/v1/finalcut/copy-jobs/9999/remark", json={"remark": "随便写点"}
        )
        assert response.status_code == 404
        assert response.json()["success"] is False


class TestCopyJobSubtitleText:
    """第 ② 步的字幕对照：原文（磁盘产物）+ 素材（喂给 AI 的那份）。

    这条接口的意义是让用户看出「AI 是没读懂素材，还是读懂了但写得差」，
    所以**任务的状态不影响能不能取**：失败与取消时恰恰最需要看 AI 读到了什么。
    """

    def test_returns_content_and_material(self, client, media_files):
        subtitle, _ = media_files
        created = _create_job(client, media_files)

        response = client.get(f"/api/v1/finalcut/copy-jobs/{created['id']}/subtitle-text")
        assert response.status_code == 200, response.text
        body = response.json()["data"]

        assert body["path"] == str(subtitle)
        assert body["name"] == subtitle.name
        assert body["size_bytes"] == subtitle.stat().st_size
        # 原文是磁盘上那份：带序号与时间轴
        assert "00:00:00,000 --> 00:00:02,000" in body["content"]
        assert "这个收纳架真的绝了" in body["content"]
        # 素材是喂给 AI 的那份：时间轴与序号都剥掉了
        assert "-->" not in body["material"]
        assert body["material"] == "这个收纳架真的绝了"
        assert body["truncated"] is False
        assert body["material_truncated"] is False

    def test_material_is_deduped_and_ass_stripped(self, client, db_session, tmp_path):
        """素材视图必须走后端拆解：连续重复行合并、ASS 行内标签与非 Events 段剥掉。

        前端那份「去时间轴」的文本变换做不到这些，所以这个视图只能由后端给。
        """
        from app.models.finalcut_job import FinalcutCopyJob

        ass = tmp_path / "口播.ass"
        ass.write_text(
            "[Script Info]\nTitle: 带货口播\n\n"
            "[V4+ Styles]\nStyle: Default,微软雅黑\n\n"
            "[Events]\n"
            "Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,{\\an8}这个收纳架真的绝了\n"
            "Dialogue: 0,0:00:02.00,0:00:04.00,Default,,0,0,0,,这个收纳架真的绝了\n"
            "Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,一放就整整齐齐\n",
            encoding="utf-8",
        )
        job = FinalcutCopyJob(
            status="failed", subtitle_path=str(ass), video_path=str(tmp_path / "成片.mp4"),
            video_duration=15.0, copy_count=5, hint="", model="deepseek-chat",
            error_message="AI 调用失败",
        )
        db_session.add(job)
        db_session.commit()

        body = client.get(f"/api/v1/finalcut/copy-jobs/{job.id}/subtitle-text").json()["data"]

        assert body["material"] == "这个收纳架真的绝了\n一放就整整齐齐"
        assert "Dialogue:" not in body["material"]
        assert "{" not in body["material"]
        assert "Style:" not in body["material"]
        # 原文视图保持磁盘上的原文，不做任何加工
        assert "Dialogue: 0,0:00:00.00" in body["content"]
        assert "[Script Info]" in body["content"]

    def test_content_truncates_at_block_boundary(
        self, client, media_files, monkeypatch, tmp_path
    ):
        """原文超上限：截断 + 标记，且结尾不剩半个字幕块。"""
        monkeypatch.setattr(settings, "SUBTITLE_PREVIEW_MAX_BYTES", 60)
        subtitle = tmp_path / "长字幕.srt"
        subtitle.write_text(
            "".join(
                f"{i}\n00:00:0{i},000 --> 00:00:0{i},500\n第 {i} 句口播内容\n\n"
                for i in range(1, 9)
            ),
            encoding="utf-8",
        )
        _, video = media_files
        created = _create_job(client, media_files, subtitle_path=str(subtitle))

        body = client.get(
            f"/api/v1/finalcut/copy-jobs/{created['id']}/subtitle-text"
        ).json()["data"]

        assert body["truncated"] is True
        assert body["size_bytes"] > 60
        # 结尾是完整的一句，没有半截的时间轴行
        assert body["content"].endswith("第 1 句口播内容")
        # 原文被截断**不影响**素材：素材按整份文件拆解，这里是完整的八句
        assert body["material"].splitlines()[0] == "第 1 句口播内容"
        assert body["material"].splitlines()[-1] == "第 8 句口播内容"
        assert body["material_truncated"] is False
        assert video.is_file()  # 成片没被这件事动过

    def test_material_truncated_flag(self, client, media_files, monkeypatch):
        """素材超上限：AI 当时也只读到了这些（与原文超限是两回事）。"""
        monkeypatch.setattr(settings, "FINALCUT_SRT_MAX_CHARS", 5)
        created = _create_job(client, media_files)

        body = client.get(
            f"/api/v1/finalcut/copy-jobs/{created['id']}/subtitle-text"
        ).json()["data"]

        assert body["material_truncated"] is True
        assert "已省略" in body["material"]
        assert body["truncated"] is False

    @pytest.mark.parametrize("status", ["pending", "running", "success", "cancelled"])
    def test_any_status_can_be_read(self, client, media_files, db_session, status):
        """任务没跑完 / 被取消 / 已成功，都取得到字幕 —— 状态不参与判断。"""
        from app.models.finalcut_job import FinalcutCopyJob

        subtitle, video = media_files
        job = FinalcutCopyJob(
            status=status, subtitle_path=str(subtitle), video_path=str(video),
            video_duration=15.0, copy_count=5, hint="", model="deepseek-chat",
        )
        db_session.add(job)
        db_session.commit()

        response = client.get(f"/api/v1/finalcut/copy-jobs/{job.id}/subtitle-text")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["material"] == "这个收纳架真的绝了"

    def test_ignores_path_query_param(self, client, media_files, tmp_path):
        """路径只认任务记录里的那条：带 ?path= 也不读第二个文件。

        本接口是「按任务 id 取字幕」的窄口子，放开读任意路径就是任意文件读取。
        """
        other = tmp_path / "别人的字幕.srt"
        other.write_text("1\n00:00:00,000 --> 00:00:01,000\n不该被读到\n", encoding="utf-8")
        created = _create_job(client, media_files)

        response = client.get(
            f"/api/v1/finalcut/copy-jobs/{created['id']}/subtitle-text",
            params={"path": str(other)},
        )
        assert response.status_code == 200, response.text
        body = response.json()["data"]
        assert body["path"] == str(media_files[0])
        assert "不该被读到" not in body["content"]

    def test_deleted_file_404(self, client, media_files):
        """字幕文件被搬走：404 而不是 500（页面上由卡片内提示呈现）。"""
        created = _create_job(client, media_files)
        media_files[0].unlink()

        response = client.get(f"/api/v1/finalcut/copy-jobs/{created['id']}/subtitle-text")
        assert response.status_code == 404
        assert response.json()["success"] is False

    def test_missing_job_404(self, client):
        response = client.get("/api/v1/finalcut/copy-jobs/9999/subtitle-text")
        assert response.status_code == 404

    def test_job_id_zero_422(self, client):
        assert client.get("/api/v1/finalcut/copy-jobs/0/subtitle-text").status_code == 422


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


class TestRenderJobRemark:
    """合成任务的备注与文案任务同一套契约（共用 JobRemarkUpdate 模型）。"""

    def test_save_echoes_in_detail_and_list(self, client, media_files):
        created = _create_render_job(client, media_files)
        response = client.put(
            f"/api/v1/finalcut/render-jobs/{created['id']}/remark",
            json={"remark": "等审核通过再发"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["remark"] == "等审核通过再发"

        detail = client.get(f"/api/v1/finalcut/render-jobs/{created['id']}").json()["data"]
        assert detail["remark"] == "等审核通过再发"
        items = client.get("/api/v1/finalcut/render-jobs").json()["data"]["items"]
        assert items[0]["remark"] == "等审核通过再发"

    def test_empty_string_clears_remark(self, client, media_files):
        created = _create_render_job(client, media_files)
        path = f"/api/v1/finalcut/render-jobs/{created['id']}/remark"
        client.put(path, json={"remark": "先写一条"})

        response = client.put(path, json={"remark": ""})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["remark"] == ""
        detail = client.get(f"/api/v1/finalcut/render-jobs/{created['id']}").json()["data"]
        assert detail["remark"] == ""

    def test_remark_too_long_422(self, client, media_files):
        created = _create_render_job(client, media_files)
        response = client.put(
            f"/api/v1/finalcut/render-jobs/{created['id']}/remark",
            json={"remark": "长" * (MAX_JOB_REMARK_LENGTH + 1)},
        )
        assert response.status_code == 422

    def test_remark_missing_job_404(self, client):
        response = client.put(
            "/api/v1/finalcut/render-jobs/9999/remark", json={"remark": "随便写点"}
        )
        assert response.status_code == 404
        assert response.json()["success"] is False


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
            remark="客户 A 那版",
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
            remark="口播初版",
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
        # 备注挂在任务表上，产物清单要 join 回任务表把它带出来 —— 清单里几条
        # 「字幕 #12 / a.srt」长得一模一样，备注才是用户认得出的标记。
        assert entry["remark"] == "客户 A 那版"

        assert len(data["subtitles"]) == 1
        assert data["subtitles"][0]["origin"] == "subtitle"
        assert data["subtitles"][0]["video_url"] == ""
        assert data["subtitles"][0]["remark"] == "口播初版"
        assert data["subtitles"][0]["index"] == 1

    def test_source_index_actually_previews_that_subtitle(
        self, client, db_session, tmp_path
    ):
        """清单里的 index 要能直接喂给字幕预览接口。

        这是两个接口之间的契约：清单用 (job_id, index) 描述一份字幕，预览接口
        也用 (job_id, index) 取内容。index 写错（0 起算、或按「清单里的位置」
        重排）**不会报错**，只会静默预览到别人的字幕。

        所以第一条刻意造成**失败**（进不了清单，但仍在任务里占着序号 1）：
        清单里第二条的位置是 1，而它的真实序号是 2 —— 只写「位置」的实现会
        预览到 1.srt 去。
        """
        from app.models.subtitle_job import (
            SubtitleJob,
            SubtitleJobItem,
            SubtitleJobItemStatus,
        )

        sub = SubtitleJob(
            status="success", input_path=str(tmp_path), output_dir=str(tmp_path),
        )
        db_session.add(sub)
        db_session.flush()
        paths = []
        for index in (1, 2):
            srt = tmp_path / f"{index}.srt"
            srt.write_text(
                f"1\n00:00:00,000 --> 00:00:02,000\n第{index}条\n", encoding="utf-8"
            )
            paths.append(srt)
            ok = index == 2
            db_session.add(
                SubtitleJobItem(
                    job_id=sub.id, index=index, source_path=str(tmp_path),
                    source_name=f"视频{index}.mp4", output_path=str(srt),
                    status=(
                        SubtitleJobItemStatus.SUCCESS if ok
                        else SubtitleJobItemStatus.FAILED
                    ),
                    subtitle_exists=ok, file_size=20,
                )
            )
        db_session.commit()

        sources = client.get("/api/v1/finalcut/sources").json()["data"]["subtitles"]
        assert [item["path"] for item in sources] == [str(paths[1])]
        entry = sources[0]
        assert entry["index"] == 2  # 真实序号，不是它在清单里的位置

        preview = client.get(
            f"/api/v1/subtitle/jobs/{sub.id}/subtitles/{entry['index']}"
        ).json()["data"]
        assert "第2条" in preview["content"]
        assert preview["name"] == "2.srt"

    def test_sources_without_remark_gives_empty_string(self, client, db_session, tmp_path):
        """没写备注的任务 → 空串（而不是缺键）：前端按「空串 = 没写」判断。"""
        from app.models.subtitle_job import (
            SubtitleJob,
            SubtitleJobItem,
            SubtitleJobItemStatus,
        )

        srt = tmp_path / "无备注.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
        sub = SubtitleJob(
            status="success", input_path=str(tmp_path), output_dir=str(tmp_path),
        )
        db_session.add(sub)
        db_session.flush()
        db_session.add(
            SubtitleJobItem(
                job_id=sub.id, index=1, source_path=str(srt), source_name="成片.mp4",
                output_path=str(srt), status=SubtitleJobItemStatus.SUCCESS,
                subtitle_exists=True, file_size=20,
            )
        )
        db_session.commit()

        entry = client.get("/api/v1/finalcut/sources").json()["data"]["subtitles"][0]
        assert entry["remark"] == ""

    def test_sources_empty(self, client):
        data = client.get("/api/v1/finalcut/sources").json()["data"]
        assert data == {"subtitles": [], "videos": []}


# 本地视频预览流的测试在 tests/test_fs.py —— 接口已统一到 GET /fs/preview
# （跨功能公共设施，一键成品的框选步骤与镜头分割的素材列表共用同一条流）。
