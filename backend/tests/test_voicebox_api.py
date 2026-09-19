"""智能配音接口（api/v1/voicebox.py）的测试。

三个隔离点，缺一不可：
1. **上游 HTTP 全部换成假函数**（monkeypatch app.services.voicebox_client 的四个
   函数）—— 测试不该依赖开发机上真的开着 Voicebox，更不该跑真实生成；
2. **素材根指到 tmp**（conftest 没有 autouse 的素材根隔离）—— 否则配音产物
   会写进真实的 materials/dubbing/；
3. **.env 指到 tmp**（monkeypatch voicebox_settings._ENV_PATH）—— 「指定服务
   地址」这条用例会真写文件，绝不能碰真的 backend/.env。

生成是后台 worker 跑的，所以「生成全流程」这类用例靠轮询到终态
（真实 worker 线程 + 假的上游函数，逻辑路径与生产完全一致）。
"""

import time

import pytest

from app.core.config import settings
from app.services import voicebox_env, voicebox_generation, voicebox_settings
from app.services.voicebox_client import VoiceboxError

UPSTREAM_AUDIO = b"RIFF\x00\x00\x00\x00WAVEfake-audio-bytes"

HEALTH_READY = {
    "reachable": True,
    "status": "ready",
    "model_loaded": True,
    "model_downloaded": True,
    "model_size": "1.7B",
    "gpu_available": True,
    "vram_used_mb": 1024.0,
    "detail": "服务正常",
}

HEALTH_UNREACHABLE = {
    "reachable": False,
    "detail": "连不上 Voicebox 服务（http://127.0.0.1:17493）：请先打开 Voicebox 桌面端",
}

PROFILES = [
    {"id": "p1", "name": "我的声音", "description": "克隆自本人", "language": "zh"},
    {"id": "p2", "name": "女声解说", "description": None, "language": "zh"},
]


@pytest.fixture()
def vb(monkeypatch, tmp_path):
    """把 Voicebox 的三处外部依赖都换成假的，并清干净进程内缓存/注册表。

    缓存（voicebox_env）与注册表（voicebox_generation）都是模块级全局状态，
    用例之间必须清 —— 否则前一条用例的探测结果会被后一条读到。
    """
    materials = tmp_path / "materials"
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(materials))
    monkeypatch.setattr(settings, "VOICEBOX_BASE_URL", "http://127.0.0.1:17493")
    monkeypatch.setattr(voicebox_settings, "_ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("VOICEBOX_BASE_URL", raising=False)

    voicebox_env.reset_cache()
    voicebox_generation._reset_for_tests()
    try:
        yield materials
    finally:
        voicebox_env.reset_cache()
        voicebox_generation._reset_for_tests()


@pytest.fixture()
def fake_upstream(monkeypatch):
    """假的 Voicebox 服务端。返回一个可改字段的字典，用例按需覆写。"""
    state = {
        "health": dict(HEALTH_READY),
        "profiles": list(PROFILES),
        "profiles_error": None,
        "generate_result": {"id": "gen_1", "duration": 3.5, "audio_path": "/up/gen_1.wav"},
        "generate_error": None,
        "audio": UPSTREAM_AUDIO,
    }

    def probe(*, config=None, transport=None):
        return dict(state["health"])

    def list_profiles(*, config=None, transport=None):
        if state["profiles_error"] is not None:
            raise state["profiles_error"]
        return [dict(item) for item in state["profiles"]]

    def generate(text, *, profile_id, language="zh", model_size="1.7B", config=None, transport=None):
        if state["generate_error"] is not None:
            raise state["generate_error"]
        return dict(state["generate_result"])

    def fetch_audio(generation_id, *, hint="", config=None, transport=None):
        # 与真客户端同样在「空音频」上直接报错：字节还没落盘就该失败，
        # 不该先写出一份空文件再补救
        if not state["audio"]:
            raise VoiceboxError("bad_response", "Voicebox 返回了空音频")
        return state["audio"], ".wav"

    monkeypatch.setattr("app.services.voicebox_client.probe", probe)
    monkeypatch.setattr("app.services.voicebox_client.list_profiles", list_profiles)
    monkeypatch.setattr("app.services.voicebox_client.generate", generate)
    monkeypatch.setattr("app.services.voicebox_client.fetch_audio", fetch_audio)
    return state


def _environment(client, refresh: bool = False) -> dict:
    response = client.get("/api/v1/voicebox/environment", params={"refresh": refresh})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _wait_terminal(client, generation_id: int, timeout: float = 10.0) -> dict:
    """轮询到终态（真实 worker 在跑，只是上游是假的）。"""
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/voicebox/generations/{generation_id}")
        assert response.status_code == 200, response.text
        last = response.json()["data"]
        if last["status"] in ("success", "failed"):
            return last
        time.sleep(0.05)
    raise AssertionError(f"等不到终态，最后一条记录是 {last}")


class TestEnvironment:
    def test_unreachable_is_200_with_hints(self, client, vb, fake_upstream):
        """没开 Voicebox 是正常状态，不是 500。"""
        fake_upstream["health"] = dict(HEALTH_UNREACHABLE)

        data = _environment(client)

        assert data["ready"] is False
        assert data["reachable"] is False
        assert data["base_url"] == "http://127.0.0.1:17493"
        assert data["base_url_source"] == "default"
        assert data["install_hints"], "连不上时必须给出分步指引"
        assert data["fix_hint"]
        assert "连不上" in data["detail"]
        # 指引只是文本，后端不代装；下载链接直接指向 GitHub Releases
        assert all(
            "github.com" in hint["url"] or not hint["url"] for hint in data["install_hints"]
        )

    def test_model_not_downloaded_is_not_ready(self, client, vb, fake_upstream):
        fake_upstream["health"] = {**HEALTH_READY, "model_downloaded": False}

        data = _environment(client)

        assert data["reachable"] is True
        assert data["ready"] is False
        assert "模型" in data["fix_hint"]
        assert any("模型" in warning for warning in data["warnings"])
        # 服务是通的，就不该再甩安装指引
        assert data["install_hints"] == []

    def test_ready_reports_profiles_and_gpu(self, client, vb, fake_upstream):
        data = _environment(client)

        assert data["ready"] is True
        assert data["profile_count"] == 2
        assert data["gpu_available"] is True
        assert data["vram_used_mb"] == 1024.0
        assert data["default_output_dir"].endswith("dubbing")
        assert data["warnings"] == []

    def test_no_gpu_only_warns(self, client, vb, fake_upstream):
        fake_upstream["health"] = {**HEALTH_READY, "gpu_available": False}

        data = _environment(client)

        assert data["ready"] is True  # 没显卡也能跑，只是慢
        assert any("GPU" in warning for warning in data["warnings"])

    def test_environment_cache_holds_until_refresh(self, client, vb, fake_upstream):
        assert _environment(client)["profile_count"] == 2
        fake_upstream["profiles"] = []
        assert _environment(client)["profile_count"] == 2  # 命中缓存
        assert _environment(client, refresh=True)["profile_count"] == 0

    def test_profile_list_failure_is_a_warning_not_a_failure(self, client, vb, fake_upstream):
        """服务在但音色拉不到：不推翻「服务可用」，只提醒一句。"""
        fake_upstream["profiles_error"] = VoiceboxError("http", "Voicebox 拒绝了这次请求")

        data = _environment(client)

        assert data["reachable"] is True
        assert data["ready"] is True
        assert any("音色列表" in warning for warning in data["warnings"])


class TestBaseUrl:
    def test_writes_env_and_returns_fresh_probe(self, client, vb, fake_upstream):
        response = client.put(
            "/api/v1/voicebox/environment/base-url",
            json={"base_url": "192.168.1.9:17493"},
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["base_url"] == "http://192.168.1.9:17493"
        assert data["base_url_source"] == "env_file"

        env_file = vb.parent / ".env"
        assert "VOICEBOX_BASE_URL=http://192.168.1.9:17493" in env_file.read_text("utf-8")
        assert settings.VOICEBOX_BASE_URL == "http://192.168.1.9:17493"

    def test_empty_restores_default(self, client, vb, fake_upstream):
        client.put(
            "/api/v1/voicebox/environment/base-url",
            json={"base_url": "http://192.168.1.9:17493"},
        )
        data = client.put(
            "/api/v1/voicebox/environment/base-url", json={"base_url": ""}
        ).json()["data"]

        assert data["base_url"] == "http://127.0.0.1:17493"
        assert data["base_url_source"] == "default"

    def test_invalid_scheme_is_400(self, client, vb, fake_upstream):
        response = client.put(
            "/api/v1/voicebox/environment/base-url",
            json={"base_url": "file:///etc/passwd"},
        )
        assert response.status_code == 400
        assert "http" in response.json()["error"]["message"]

    def test_newline_is_400_and_env_untouched(self, client, vb, fake_upstream):
        """换行绝不能落到 .env 里（会注入出一行新配置）。"""
        response = client.put(
            "/api/v1/voicebox/environment/base-url",
            json={"base_url": "http://a.com\nEVIL=1"},
        )
        assert response.status_code == 400
        assert not (vb.parent / ".env").exists()

    def test_missing_hostname_is_400(self, client, vb, fake_upstream):
        response = client.put(
            "/api/v1/voicebox/environment/base-url", json={"base_url": "http://"}
        )
        assert response.status_code == 400


class TestProfiles:
    def test_lists_profiles(self, client, vb, fake_upstream):
        response = client.get("/api/v1/voicebox/profiles")

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["total"] == 2
        assert data["items"][0]["name"] == "我的声音"

    def test_upstream_error_is_400_with_readable_message(self, client, vb, fake_upstream):
        fake_upstream["generate_error"] = None
        fake_upstream["profiles_error"] = VoiceboxError(
            "unreachable", "连不上 Voicebox 服务：请先打开 Voicebox 桌面端"
        )

        response = client.get("/api/v1/voicebox/profiles")

        assert response.status_code == 400
        assert "请先打开 Voicebox" in response.json()["error"]["message"]


class TestGeneration:
    def test_full_flow_writes_artifact_and_index(self, client, vb, fake_upstream):
        response = client.post(
            "/api/v1/voicebox/generations",
            json={
                "text": "大家好，今天推荐一款保温杯",
                "profile_id": "p1",
                "profile_name": "我的声音",
                "filename": "保温杯开场",
                "language": "zh",
                "model_size": "1.7B",
            },
        )
        assert response.status_code == 201, response.text
        created = response.json()["data"]
        assert created["status"] in ("queued", "running")
        assert created["text_excerpt"].startswith("大家好")

        finished = _wait_terminal(client, created["id"])
        assert finished["status"] == "success", finished
        assert finished["duration"] == 3.5
        assert finished["error_message"] == ""
        assert finished["elapsed_seconds"] >= 0

        output = vb / "dubbing" / "保温杯开场.wav"
        assert output.is_file()
        assert output.read_bytes() == UPSTREAM_AUDIO

        # 产物清单：磁盘 + 索引合起来看
        listing = client.get("/api/v1/voicebox/audios").json()["data"]
        assert listing["total"] == 1
        item = listing["items"][0]
        assert item["name"] == "保温杯开场.wav"
        assert item["indexed"] is True
        assert item["profile_name"] == "我的声音"
        assert item["duration"] == 3.5
        assert item["size_bytes"] == len(UPSTREAM_AUDIO)
        assert item["audio_url"].startswith("/api/v1/voicebox/audios/")
        assert listing["dir"].endswith("dubbing") or listing["dir"].endswith("dubbing\\")

        # 索引文件落在产物目录里（重启后清单还能带上音色与时长）
        index = vb / "dubbing" / ".dubbing-index.json"
        assert index.is_file()
        assert "保温杯开场.wav" in index.read_text("utf-8")

    def test_name_collision_gets_numbered_suffix(self, client, vb, fake_upstream):
        for _ in range(2):
            created = client.post(
                "/api/v1/voicebox/generations",
                json={"text": "同一段文案", "profile_id": "p1", "filename": "重复"},
            ).json()["data"]
            assert _wait_terminal(client, created["id"])["status"] == "success"

        names = {item["name"] for item in client.get("/api/v1/voicebox/audios").json()["data"]["items"]}
        assert names == {"重复.wav", "重复-2.wav"}

    def test_empty_filename_gets_timestamp_name(self, client, vb, fake_upstream):
        created = client.post(
            "/api/v1/voicebox/generations",
            json={"text": "没给文件名", "profile_id": "p1"},
        ).json()["data"]
        finished = _wait_terminal(client, created["id"])

        assert finished["status"] == "success"
        assert finished["filename"].startswith("dub_")
        assert finished["filename"].endswith(".wav")

    def test_upstream_failure_marks_failed_with_message(self, client, vb, fake_upstream):
        fake_upstream["generate_error"] = VoiceboxError(
            "unreachable", "连不上 Voicebox 服务（http://127.0.0.1:17493）：请先打开 Voicebox 桌面端"
        )

        created = client.post(
            "/api/v1/voicebox/generations",
            json={"text": "这段会失败", "profile_id": "p1", "filename": "失败样本"},
        ).json()["data"]
        finished = _wait_terminal(client, created["id"])

        assert finished["status"] == "failed"
        assert "请先打开 Voicebox" in finished["error_message"]
        # 失败不留半截产物
        assert not (vb / "dubbing" / "失败样本.wav").exists()
        assert client.get("/api/v1/voicebox/audios").json()["data"]["total"] == 0

    def test_empty_audio_marks_failed(self, client, vb, fake_upstream):
        fake_upstream["audio"] = b""

        created = client.post(
            "/api/v1/voicebox/generations",
            json={"text": "上游返回空音频", "profile_id": "p1"},
        ).json()["data"]
        finished = _wait_terminal(client, created["id"])

        assert finished["status"] == "failed"
        assert client.get("/api/v1/voicebox/audios").json()["data"]["total"] == 0

    def test_text_too_long_is_422(self, client, vb, fake_upstream):
        response = client.post(
            "/api/v1/voicebox/generations",
            json={"text": "字" * (settings.VOICEBOX_MAX_TEXT_CHARS + 1), "profile_id": "p1"},
        )
        assert response.status_code == 422

    def test_blank_text_is_400(self, client, vb, fake_upstream):
        response = client.post(
            "/api/v1/voicebox/generations", json={"text": "   ", "profile_id": "p1"}
        )
        assert response.status_code == 400

    def test_unsupported_language_is_400(self, client, vb, fake_upstream):
        """上游只认 zh|en，与其等它报错，不如先拦住。"""
        response = client.post(
            "/api/v1/voicebox/generations",
            json={"text": "hello", "profile_id": "p1", "language": "ja"},
        )
        assert response.status_code == 400

    def test_unsupported_model_size_is_400(self, client, vb, fake_upstream):
        response = client.post(
            "/api/v1/voicebox/generations",
            json={"text": "hello", "profile_id": "p1", "model_size": "7B"},
        )
        assert response.status_code == 400

    def test_queue_full_is_400(self, client, vb, fake_upstream, monkeypatch):
        """挂着的任务堆到上限就拒绝，而不是无声排到天荒地老。"""
        # 既不起 worker，也不让（可能已存在的）worker 真执行：记录因此一直停在
        # queued，pending 数只会涨 —— 这是「队列满」的唯一稳定造法
        monkeypatch.setattr(voicebox_generation, "_ensure_worker", lambda: None)
        monkeypatch.setattr(voicebox_generation, "_run", lambda record_id: None)
        monkeypatch.setattr(settings, "VOICEBOX_MAX_PENDING", 2)

        for _ in range(2):
            assert client.post(
                "/api/v1/voicebox/generations",
                json={"text": "排队", "profile_id": "p1"},
            ).status_code == 201

        response = client.post(
            "/api/v1/voicebox/generations", json={"text": "排队", "profile_id": "p1"}
        )
        assert response.status_code == 400
        assert "上限" in response.json()["error"]["message"]

    def test_unknown_generation_is_404(self, client, vb, fake_upstream):
        response = client.get("/api/v1/voicebox/generations/999")
        assert response.status_code == 404


class TestAudioAccess:
    @pytest.fixture()
    def generated(self, client, vb, fake_upstream) -> dict:
        """先生成一份产物，供访问/删除类用例使用。"""
        created = client.post(
            "/api/v1/voicebox/generations",
            json={"text": "访问用样本", "profile_id": "p1", "profile_name": "我的声音", "filename": "样本"},
        ).json()["data"]
        finished = _wait_terminal(client, created["id"])
        assert finished["status"] == "success", finished
        return {"vb": vb, "record": finished}

    def test_audio_file_served(self, client, generated):
        response = client.get("/api/v1/voicebox/audios/样本.wav/file")

        assert response.status_code == 200, response.text
        assert response.content == UPSTREAM_AUDIO
        assert response.headers["content-type"].startswith("audio/wav")

    def test_audio_file_supports_range(self, client, generated):
        response = client.get(
            "/api/v1/voicebox/audios/样本.wav/file", headers={"Range": "bytes=0-3"}
        )

        assert response.status_code == 206
        assert response.content == UPSTREAM_AUDIO[:4]

    def test_unknown_audio_is_404(self, client, generated):
        assert client.get("/api/v1/voicebox/audios/不存在.wav/file").status_code == 404

    def test_non_audio_extension_is_400(self, client, generated):
        """只认音频白名单，顺手挡住拿别的扩展名来试探的。"""
        assert client.get("/api/v1/voicebox/audios/notes.txt/file").status_code == 400

    def test_path_traversal_is_blocked(self, client, generated):
        """路径穿越钉死：带目录成分的名字一律走不到文件。

        两道闸口，404 与 400 都算拦住：
        - `%2F` 会被 Starlette 在路由匹配前解码成 `/`，于是根本没有路由能匹配
          （404）—— 也就是说这类尝试连处理函数都进不去；
        - 反斜杠、`..` 这类能进处理函数的，由 dubbing_library.resolve_audio
          判成非法名字（400）。
        """
        for name in ("..%2Fsecret.wav", "%2Fetc%2Fpasswd.wav", "..%5Csecret.wav", "%2E%2E.wav"):
            response = client.get(f"/api/v1/voicebox/audios/{name}/file")
            assert response.status_code in (400, 404), (
                f"{name} 应该被拦住，却返回 {response.status_code}"
            )
            assert response.status_code != 200

    def test_delete_removes_file_and_index_entry(self, client, generated):
        vb = generated["vb"]
        target = vb / "dubbing" / "样本.wav"
        assert target.is_file()

        response = client.delete("/api/v1/voicebox/audios/样本.wav")
        assert response.status_code == 200, response.text
        assert not target.exists()
        assert client.get("/api/v1/voicebox/audios").json()["data"]["total"] == 0

        index_text = (vb / "dubbing" / ".dubbing-index.json").read_text("utf-8")
        assert "样本.wav" not in index_text

    def test_delete_unknown_is_404(self, client, generated):
        assert client.delete("/api/v1/voicebox/audios/没有这个.wav").status_code == 404

    def test_delete_traversal_is_blocked(self, client, generated):
        assert client.delete("/api/v1/voicebox/audios/..%5Csecret.wav").status_code == 400


class TestAudioListingWithoutUpstream:
    def test_user_copied_file_is_listed_but_not_indexed(self, client, vb, fake_upstream):
        """用户自己往 materials/dubbing/ 拷的音频也要列出来（磁盘是唯一真相）。"""
        dubbing = vb / "dubbing"
        dubbing.mkdir(parents=True, exist_ok=True)
        (dubbing / "外部素材.mp3").write_bytes(b"ID3fake")

        listing = client.get("/api/v1/voicebox/audios").json()["data"]

        assert listing["total"] == 1
        item = listing["items"][0]
        assert item["name"] == "外部素材.mp3"
        assert item["indexed"] is False
        assert item["duration"] is None
        assert item["profile_name"] == ""

    def test_missing_dir_returns_empty(self, client, vb, fake_upstream):
        """产物目录还不存在时返回空清单，不报错、也不顺手建目录。"""
        listing = client.get("/api/v1/voicebox/audios").json()["data"]

        assert listing["total"] == 0
        assert not (vb / "dubbing").exists()

    def test_corrupt_index_falls_back_to_scan(self, client, vb, fake_upstream):
        dubbing = vb / "dubbing"
        dubbing.mkdir(parents=True, exist_ok=True)
        (dubbing / "坏索引.wav").write_bytes(b"RIFF")
        (dubbing / ".dubbing-index.json").write_text("{ 这不是 JSON", encoding="utf-8")

        listing = client.get("/api/v1/voicebox/audios").json()["data"]

        assert listing["total"] == 1
        assert listing["items"][0]["indexed"] is False
