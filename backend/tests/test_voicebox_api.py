"""智能配音接口（api/v1/voicebox.py）的测试。

隔离点，缺一不可：
1. **上游 HTTP 全部换成假函数**（monkeypatch app.services.voicebox_client 的五个
   函数：probe / list_profiles / list_models / generate / fetch_audio）——
   测试不该依赖开发机上真的开着 Voicebox，更不该跑真实生成；
2. **素材根指到 tmp**（conftest 没有 autouse 的素材根隔离）—— 否则配音产物
   会写进真实的 materials/dubbing/；
3. **.env 指到 tmp**（monkeypatch voicebox_settings._ENV_PATH）—— 「指定服务
   地址」这条用例会真写文件，绝不能碰真的 backend/.env；
4. **用户级环境变量与进程管理全部隔离**（`vb` 夹具里那几行）—— 「设置镜像」
   在 macOS 上会写真的 `~/Library/LaunchAgents/` 并调 `launchctl setenv`，
   「重启 Voicebox」会真的退出用户开着的 Voicebox。自检本身也会去 `stat`
   若干安装路径、跑一次 Spotlight。这三件事一件都不能真发生。

第 4 条只改「外部世界」，**不替换被测代码**：请求照常穿过 API → service →
（假的）launchctl / taskkill，只是最外面那层换成了记录器。

生成是后台 worker 跑的，所以「生成全流程」这类用例靠轮询到终态
（真实 worker 线程 + 假的上游函数，逻辑路径与生产完全一致）。
"""

import time

import pytest

from types import SimpleNamespace

from app.core.config import settings
from app.services import (
    user_env,
    voicebox_env,
    voicebox_generation,
    voicebox_mirror,
    voicebox_restart,
    voicebox_settings,
)
from app.services.voicebox_client import VoiceboxError
from tests.fakes import FakeCtypes, FakeLaunchctl, FakeWinreg

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

#: 上游 /models/status 的形状：**全部**模型混在一起（TTS + whisper 转写 + qwen3
#: 大模型），这正是后端要筛一遍的理由。两条 qwen-tts 一好一坏，覆盖两种状态。
UPSTREAM_MODELS = [
    {
        "model_name": "qwen-tts-1.7B",
        "display_name": "Qwen TTS 1.7B",
        "downloaded": False,
        "downloading": False,
        "loaded": False,
        "size_mb": None,
    },
    {
        "model_name": "qwen-tts-0.6B",
        "display_name": "Qwen TTS 0.6B",
        "downloaded": True,
        "downloading": False,
        "loaded": True,
        "size_mb": 2399.5,
    },
    {
        "model_name": "kokoro",
        "display_name": "Kokoro 82M",
        "downloaded": True,
        "downloading": False,
        "loaded": False,
        "size_mb": 320.0,
    },
    # 下面两个不是配音模型，不该出现在 /voicebox/models 里
    {
        "model_name": "whisper-base",
        "display_name": "Whisper Base",
        "downloaded": True,
        "downloading": False,
        "loaded": False,
        "size_mb": 150.0,
    },
    {
        "model_name": "qwen3-4b",
        "display_name": "Qwen3 4B",
        "downloaded": True,
        "downloading": False,
        "loaded": False,
        "size_mb": 8000.0,
    },
]


class Platform:
    """可切换的平台：默认钉成 macOS（本机），要测别的系统的用例改 `key`。

    存在的理由：`platform_key` 被三个模块用裸名导入（`from ... import platform_key`），
    所以得逐个模块 patcha —— 换成一个共享的可调用对象，改一处就全改到。
    这样「Linux 上不支持」这类用例不再取决于跑测试的机器是什么系统。
    """

    def __init__(self, key: str = "macos") -> None:
        self.key = key

    def __call__(self) -> str:
        return self.key


@pytest.fixture()
def vb(monkeypatch, tmp_path):
    """把 Voicebox 的外部依赖（上游 HTTP、文件系统、系统环境变量、进程）全换成假的，
    并清干净进程内缓存/注册表。

    缓存（voicebox_env）与注册表（voicebox_generation）都是模块级全局状态，
    用例之间必须清 —— 否则前一条用例的探测结果会被后一条读到。

    `app_path` 给一个 tmp 下的假 `.app`：自检据此认为「能找到安装位置」，
    「重启 Voicebox」按钮在测试里默认为可用。想测「找不到」的用例自己把它换成 None。
    """
    materials = tmp_path / "materials"
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(materials))
    monkeypatch.setattr(settings, "VOICEBOX_BASE_URL", "http://127.0.0.1:17493")
    monkeypatch.setattr(voicebox_settings, "_ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("VOICEBOX_BASE_URL", raising=False)

    platform = Platform("macos")
    app = tmp_path / "Applications" / "Voicebox.app"
    app.mkdir(parents=True)
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    launchctl = FakeLaunchctl()

    # 平台判断：三个模块各改一次（它们都是裸名导入的）
    for module in (voicebox_env, user_env, voicebox_restart):
        monkeypatch.setattr(module, "platform_key", platform)
    monkeypatch.setattr(voicebox_env, "platform_label", lambda: "macOS")

    # 用户级环境变量：写进 tmp 的 LaunchAgents，launchctl 换成假的
    monkeypatch.setattr(user_env, "_run", launchctl)
    monkeypatch.setattr(user_env, "_LAUNCH_AGENTS_DIR", agents_dir)
    # Windows 那两个模块属性在非 Windows 上是 None，而有的用例会把平台改成
    # windows 来看文案 —— 不一起换掉的话会撞 AttributeError（真机上有 winreg）。
    registry = FakeWinreg()
    monkeypatch.setattr(user_env, "_winreg", registry)
    monkeypatch.setattr(user_env, "_ctypes", FakeCtypes())
    # 安装位置：不真去 stat 一堆路径、也不跑 Spotlight
    monkeypatch.setattr(voicebox_restart, "find_app", lambda: app)

    voicebox_env.reset_cache()
    voicebox_generation._reset_for_tests()
    try:
        yield SimpleNamespace(
            materials=materials,
            env_file=tmp_path / ".env",
            platform=platform,
            app=app,
            agents_dir=agents_dir,
            launchctl=launchctl,
        )
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
        "models": [dict(item) for item in UPSTREAM_MODELS],
        "models_error": None,
        "generate_result": {"id": "gen_1", "duration": 3.5, "audio_path": "/up/gen_1.wav"},
        "generate_error": None,
        "audio": UPSTREAM_AUDIO,
        #: 记下 worker 实际发给上游的参数，用来断言 engine / model_size 透传对了
        "generate_calls": [],
    }

    def probe(*, config=None, transport=None):
        return dict(state["health"])

    def list_profiles(*, config=None, transport=None):
        if state["profiles_error"] is not None:
            raise state["profiles_error"]
        return [dict(item) for item in state["profiles"]]

    def list_models(*, config=None, transport=None):
        if state["models_error"] is not None:
            raise state["models_error"]
        return [dict(item) for item in state["models"]]

    def generate(
        text,
        *,
        profile_id,
        language="zh",
        engine="qwen",
        model_size="1.7B",
        config=None,
        transport=None,
    ):
        state["generate_calls"].append(
            {"profile_id": profile_id, "language": language, "engine": engine, "model_size": model_size}
        )
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
    monkeypatch.setattr("app.services.voicebox_client.list_models", list_models)
    monkeypatch.setattr("app.services.voicebox_client.generate", generate)
    monkeypatch.setattr("app.services.voicebox_client.fetch_audio", fetch_audio)
    return state


def _environment(client, refresh: bool = False) -> dict:
    response = client.get("/api/v1/voicebox/environment", params={"refresh": refresh})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _set_base_url(client, base_url: str) -> None:
    """把服务地址改成别的机器。

    走的是**真实入口**（PUT /environment/base-url），不是直接改 settings ——
    与用户点「指定服务地址」是同一条路径，省得测出一个用户碰不到的状态。
    """
    response = client.put(
        "/api/v1/voicebox/environment/base-url", json={"base_url": base_url}
    )
    assert response.status_code == 200, response.text


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

    def test_model_not_downloaded_explains_hf_mirror(self, client, vb, fake_upstream):
        """下不动是国内最常踩的坑：提示要带上镜像的变量名与值，光说「会慢」没用。

        能由本页设置时，话要落在**按钮**上（「设置镜像」「重启 Voicebox」）——
        用户照着点就行，不必自己去改环境变量。
        """
        fake_upstream["health"] = {**HEALTH_READY, "model_downloaded": False}

        warnings = _environment(client)["warnings"]

        # 按**地址**筛而不是按 HF_ENDPOINT 这个变量名：能由本页设置时文案落在按钮上
        # （「点设置镜像」），不甩变量名给用户。变量名只在「你得手工设」时出现。
        mirror = [text for text in warnings if voicebox_mirror.HF_MIRROR_URL in text]
        assert len(mirror) == 1, f"模型没下完时该有一条镜像提示，实际 {warnings}"
        assert "设置镜像" in mirror[0], "要直接告诉用户点哪个按钮"
        assert "重启" in mirror[0], "换完镜像不重启不生效，这句不能漏"

    def test_null_model_downloaded_falls_back_to_models_status(self, client, vb, fake_upstream):
        """`/health` 在 Voicebox 0.5.0 上 model_downloaded **恒为 null**（实测：
        模型明明下好了也报 null）。不回退查 /models/status 的话，这条警告永远不
        会触发 —— 用户在一个模型都没下的机器上点生成，会撞上「上游超时」这种
        看不懂的报错（真凶是它在后台下 3.5GB）。
        """
        fake_upstream["health"] = {**HEALTH_READY, "model_downloaded": None}
        fake_upstream["models"] = [
            {**item, "downloaded": False, "loaded": False} for item in UPSTREAM_MODELS
        ]

        data = _environment(client)

        assert data["model_downloaded"] is False
        assert data["ready"] is False
        assert any("下载" in warning for warning in data["warnings"])

    def test_null_model_downloaded_with_one_downloaded_is_ready(self, client, vb, fake_upstream):
        """目录里有一个下好的就该放行 —— 用户可能就是要用那个（上游也会自己下别的）。"""
        fake_upstream["health"] = {**HEALTH_READY, "model_downloaded": None}

        data = _environment(client)

        assert data["model_downloaded"] is True
        assert data["ready"] is True

    def test_models_list_failure_does_not_block_generation(self, client, vb, fake_upstream):
        """回退查询失败就维持「不知道」—— 一次探测失败不该把用户拦在门外。

        这条守住的是自检最基本的契约：宁可放行（大不了撞上游的报错），也不能因为
        我们自己多查了一个接口，就把本来能用的环境判成不可用。
        """
        fake_upstream["health"] = {**HEALTH_READY, "model_downloaded": None}
        fake_upstream["models_error"] = VoiceboxError("http", "Voicebox 拒绝了这次请求")

        data = _environment(client)

        assert data["model_downloaded"] is None
        assert data["ready"] is True

    def test_renamed_models_do_not_read_as_not_downloaded(self, client, vb, fake_upstream):
        """上游改了 model_name（对照表全对不上）时不能判成「一个都没下」。

        那是版本差异，不是「没下载」—— 判错就会把一个模型齐全的环境锁死。
        """
        fake_upstream["health"] = {**HEALTH_READY, "model_downloaded": None}
        fake_upstream["models"] = [
            {"model_name": "qwen-tts-v2-1.7B", "downloaded": True, "loaded": True}
        ]

        data = _environment(client)

        assert data["model_downloaded"] is None
        assert data["ready"] is True

    def test_install_hints_preempt_the_hf_mirror(self, client, vb, fake_upstream):
        """第一次装的人在指引里就看见镜像，别等下到一半才踩坑。

        平台不支持时（这里钉成 Linux）改由用户手工设，文案要给出可照抄的
        `KEY=VALUE`；这跟「有按钮可点」是两句不同的话，所以分开断言。
        """
        fake_upstream["health"] = dict(HEALTH_UNREACHABLE)
        vb.platform.key = "linux"

        hints = _environment(client)["install_hints"]

        step_3 = next(hint for hint in hints if hint["title"].startswith("3."))
        assert f"{voicebox_mirror.HF_MIRROR_KEY}={voicebox_mirror.HF_MIRROR_URL}" in step_3["note"]

    def test_install_hints_point_at_the_button_when_actionable(self, client, vb, fake_upstream):
        """macOS/Windows 上装完就能一键设，指引里就该说「点设置镜像」。"""
        fake_upstream["health"] = dict(HEALTH_UNREACHABLE)

        hints = _environment(client)["install_hints"]

        step_3 = next(hint for hint in hints if hint["title"].startswith("3."))
        assert "设置镜像" in step_3["note"]
        assert voicebox_mirror.HF_MIRROR_URL in step_3["note"]

    def test_install_hints_are_platform_specific(self, client, vb, fake_upstream):
        """第 1 步给的安装包按平台不同 —— 在 macOS 上告诉用户下 .exe 是帮倒忙。"""
        fake_upstream["health"] = dict(HEALTH_UNREACHABLE)

        def step_1(platform_key: str) -> str:
            vb.platform.key = platform_key
            voicebox_env.reset_cache()
            hints = _environment(client, refresh=True)["install_hints"]
            return next(hint["note"] for hint in hints if hint["title"].startswith("1."))

        assert ".dmg" in step_1("macos")
        assert ".exe" in step_1("windows")
        assert "源码" in step_1("linux"), "Linux 没有官方包，要如实说"

    def test_ready_reports_profiles_and_gpu(self, client, vb, fake_upstream):
        data = _environment(client)

        assert data["ready"] is True
        assert data["profile_count"] == 2
        assert data["gpu_available"] is True
        assert data["vram_used_mb"] == 1024.0
        assert data["default_output_dir"].endswith("dubbing")
        assert data["warnings"] == []

    def test_no_gpu_accel_only_warns(self, client, vb, fake_upstream):
        """AMD/Intel 机器上 gpu_available 同样是 False —— 它说的是「没有能用的
        CUDA 加速」，不是「没有显卡」。文案照实说，别让人去重装显卡驱动。"""
        fake_upstream["health"] = {**HEALTH_READY, "gpu_available": False}

        data = _environment(client)

        assert data["ready"] is True  # 没有加速也能跑，只是慢
        warning = next(text for text in data["warnings"] if "GPU" in text)
        assert "NVIDIA" in warning, "要讲清它只认 CUDA，用户才知道问题出在哪"
        assert "没有检测到可用的 GPU" not in warning, "有独显的机器上这句是假话"
        assert "0.6B" in warning, "纯 CPU 的实际出路是换小模型，得写进提示里"

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

        env_file = vb.env_file
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
        assert not vb.env_file.exists()

    def test_missing_hostname_is_400(self, client, vb, fake_upstream):
        response = client.put(
            "/api/v1/voicebox/environment/base-url", json={"base_url": "http://"}
        )
        assert response.status_code == 400


class TestHfMirrorApi:
    """一键设置 / 清除模型下载源。

    这一层只验「HTTP 形状 + 编排（写环境变量 → 重新自检）」，**不验** plist / 注册表
    怎么写 —— 那是 services/user_env.py 的事，由 test_user_env.py 负责。
    """

    def test_enable_sets_the_mirror_and_returns_fresh_probe(self, client, vb, fake_upstream):
        fake_upstream["health"] = {**HEALTH_READY, "model_downloaded": False}

        response = client.put("/api/v1/voicebox/environment/hf-mirror")

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        # 响应里必须带**重新探测过**的状态，前端才不用再 GET 一次
        assert data["hf_mirror_supported"] is True
        assert data["hf_mirror_is_recommended"] is True
        assert data["hf_mirror_value"] == voicebox_mirror.HF_MIRROR_URL
        assert vb.launchctl.env["HF_ENDPOINT"] == voicebox_mirror.HF_MIRROR_URL

    def test_enable_is_idempotent(self, client, vb, fake_upstream):
        """用户多点两下不该报错，也不该攒出第二个 LaunchAgent。"""
        for _ in range(2):
            response = client.put("/api/v1/voicebox/environment/hf-mirror")
            assert response.status_code == 200, response.text

        assert response.json()["data"]["hf_mirror_is_recommended"] is True
        assert (vb.agents_dir / f"{user_env.LAUNCH_AGENT_LABEL}.plist").is_file()

    def test_disable_clears_the_mirror(self, client, vb, fake_upstream):
        client.put("/api/v1/voicebox/environment/hf-mirror")

        response = client.delete("/api/v1/voicebox/environment/hf-mirror")

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["hf_mirror_is_recommended"] is False
        assert data["hf_mirror_value"] == ""
        assert "HF_ENDPOINT" not in vb.launchctl.env

    def test_disable_without_enable_is_not_an_error(self, client, vb, fake_upstream):
        """本来就没设过 —— 「清除」的语义是「让它没有」，已经满足就别报错。"""
        assert client.delete("/api/v1/voicebox/environment/hf-mirror").status_code == 200

    def test_unsupported_platform_is_400(self, client, vb, fake_upstream):
        """Linux 上做不到 —— 这是「这台机器不支持」，不是「服务器出错」，
        用户需要的是换个做法，所以 400 而不是 500。"""
        vb.platform.key = "linux"

        response = client.put("/api/v1/voicebox/environment/hf-mirror")

        assert response.status_code == 400
        assert "手动设置" in response.json()["error"]["message"]
        assert vb.launchctl.commands == [], "不支持的系统上不该去碰 launchctl"

    def test_remote_address_is_400(self, client, vb, fake_upstream):
        """服务在另一台机器上时，本机设环境变量对它无效 —— 当场说清楚。"""
        _set_base_url(client, "http://192.168.1.9:17493")

        response = client.put("/api/v1/voicebox/environment/hf-mirror")

        assert response.status_code == 400
        assert "远程" in response.json()["error"]["message"]
        assert "HF_ENDPOINT" not in vb.launchctl.env

    def test_write_failure_is_500_with_a_code(self, client, vb, fake_upstream, monkeypatch):
        """写不进去（拿不到图形会话）是**我们这边的**故障，500 + 可定位的错误码。

        报成功的代价是用户半小时后撞上「模型还是下不动」，那时更难查。
        """
        monkeypatch.setattr(user_env, "_run", lambda argv: (1, "", "launchctl: 拒绝访问"))

        response = client.put("/api/v1/voicebox/environment/hf-mirror")

        assert response.status_code == 500
        error = response.json()["error"]
        assert error["code"] == "USER_ENV_FAILED"
        assert error["message"], "必须带上能读的说明"


class TestRestartApi:
    def test_restart_starts_the_app_and_does_not_wait(self, client, vb, fake_upstream, monkeypatch):
        """**不等就绪**：冷启动约 30 秒，同步等必然撞前端 15 秒的 fetch 超时。

        就绪交给前端轮询，所以这里的响应只承诺「已经发起重启」。
        """
        recorded = []

        def fake_restart():
            recorded.append(True)
            return voicebox_restart.RestartResult(
                app_path=str(vb.app), killed_pids=[], detail="Voicebox 已重新启动"
            )

        monkeypatch.setattr(voicebox_restart, "restart", fake_restart)

        response = client.post("/api/v1/voicebox/environment/restart")

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["started"] is True
        assert data["app_path"] == str(vb.app)
        assert data["wait_hint"], "要告诉用户「等 30 秒」，否则他以为卡住了"
        assert recorded == [True]

    def test_missing_app_is_404(self, client, vb, fake_upstream, monkeypatch):
        """找不到安装位置就如实说找不到，别假装重启了。"""
        monkeypatch.setattr(voicebox_restart, "find_app", lambda: None)
        monkeypatch.setattr(
            voicebox_restart,
            "searched_paths",
            lambda: ["/Applications/Voicebox.app"],
        )

        response = client.post("/api/v1/voicebox/environment/restart")

        assert response.status_code == 404
        assert "/Applications/Voicebox.app" in response.json()["error"]["message"]

    def test_remote_address_is_400(self, client, vb, fake_upstream):
        """本机的进程管理器重启不了另一台机器上的桌面端。"""
        _set_base_url(client, "http://192.168.1.9:17493")

        response = client.post("/api/v1/voicebox/environment/restart")

        assert response.status_code == 400
        assert "远程" in response.json()["error"]["message"]

    def test_other_restart_failure_is_500(self, client, vb, fake_upstream, monkeypatch):
        """osascript / open 真的失败了 —— 这一条不能悄悄吞掉，否则用户以为重启过了。"""
        def boom():
            raise voicebox_restart.RestartError("重启 Voicebox 失败：无法退出正在运行的实例")

        monkeypatch.setattr(voicebox_restart, "restart", boom)

        response = client.post("/api/v1/voicebox/environment/restart")

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "VOICEBOX_RESTART_FAILED"


class TestButtonAvailability:
    """自检里那 8 个字段：页面据此决定「设置镜像」「重启 Voicebox」显不显示。

    前端一句话都不判断平台，所以这里要把几个关键组合钉住。
    """

    def test_local_macos_reports_everything_supported(self, client, vb, fake_upstream):
        data = _environment(client)

        assert data["platform"] == "macos"
        assert data["platform_label"] == "macOS"
        assert data["hf_mirror_supported"] is True
        assert data["hf_mirror_is_recommended"] is False
        assert data["voicebox_app_path"] == str(vb.app)
        assert data["restart_supported"] is True

    def test_remote_address_hides_both_actions(self, client, vb, fake_upstream):
        """远程部署下这两个按钮都点不动，就别显示 —— 显示出来只会让人白点。

        模型没下完的处境下还要**说一句为什么**：用户正看着「模型下不动」的提示，
        本来该有个「设置镜像」按钮，没有就得告诉他这活儿得去那台机器上干。
        """
        fake_upstream["health"] = {**HEALTH_READY, "model_downloaded": False}
        _set_base_url(client, "http://192.168.1.9:17493")

        data = _environment(client, refresh=True)

        assert data["hf_mirror_supported"] is False
        assert data["voicebox_app_path"] == ""
        assert data["restart_supported"] is False
        assert any("远程" in warning for warning in data["warnings"])
        assert not any("设置镜像" in warning for warning in data["warnings"]), (
            "没有按钮就不能在文案里让用户去点它"
        )

    def test_remote_hides_the_button_and_stops_naming_it(self, client, vb, fake_upstream):
        """远程部署 + 本机自己设过 HF_ENDPOINT + 一个模型都没下 —— 三条同时成立。

        这是「文案指名一个页面上没有的按钮」真正会发生的组合：`hf_mirror_supported`
        为 false 时前端藏掉按钮，可本机这个变量的值照样读得到，于是旧写法会劝用户
        「点设置镜像」。远程下本机的变量根本作用不到那台机器，这条提示整个不该出现。
        """
        fake_upstream["health"] = {**HEALTH_READY, "model_downloaded": False}
        vb.launchctl.env[voicebox_mirror.HF_MIRROR_KEY] = "https://my.internal/hf"
        _set_base_url(client, "http://192.168.1.9:17493")

        data = _environment(client, refresh=True)

        assert data["hf_mirror_supported"] is False
        assert not any("设置镜像" in warning for warning in data["warnings"]), (
            "没有按钮就不能在文案里让用户去点它"
        )

    def test_local_custom_mirror_still_names_the_button(self, client, vb, fake_upstream):
        """反过来钉一下：本机能设的时候，「点设置镜像换成推荐的」必须还在。

        不加这条的话，上面那条用例只要把提示整个删掉就能过。
        """
        fake_upstream["health"] = {**HEALTH_READY, "model_downloaded": False}
        vb.launchctl.env[voicebox_mirror.HF_MIRROR_KEY] = "https://my.internal/hf"

        data = _environment(client, refresh=True)

        assert data["hf_mirror_supported"] is True
        assert any(
            "设置镜像" in warning and "my.internal" in warning
            for warning in data["warnings"]
        ), "本机设过别的源时要提醒一句、并告诉他按钮能换掉"

    def test_linux_reports_mirror_unsupported(self, client, vb, fake_upstream):
        vb.platform.key = "linux"

        data = _environment(client)

        assert data["hf_mirror_supported"] is False
        assert data["restart_supported"] is False, "Linux 上起不来桌面端"
        # 平台标签仍然要给（页面用它写「当前系统：xxx」）
        assert data["platform"] == "linux"

    def test_missing_app_hides_restart_but_keeps_the_mirror(self, client, vb, fake_upstream, monkeypatch):
        """两件事彼此独立：找不到安装位置只影响「重启」，设镜像照样能用。"""
        monkeypatch.setattr(voicebox_restart, "find_app", lambda: None)
        monkeypatch.setattr(voicebox_restart, "searched_paths", lambda: ["/Applications/Voicebox.app"])

        data = _environment(client, refresh=True)

        assert data["restart_supported"] is False
        assert data["hf_mirror_supported"] is True
        assert any("安装位置" in warning for warning in data["warnings"])

    def test_unreachable_service_still_offers_both_buttons(self, client, vb, fake_upstream):
        """Voicebox 没开着的时候正好最需要这两个按钮（「重启」等于「启动」）。"""
        fake_upstream["health"] = dict(HEALTH_UNREACHABLE)

        data = _environment(client)

        assert data["reachable"] is False
        assert data["hf_mirror_supported"] is True
        assert data["restart_supported"] is True

    def test_probe_failure_degrades_instead_of_500(self, client, vb, fake_upstream, monkeypatch):
        """自检的契约是**绝不因为探测失败而 500** —— 找不到安装位置只是少一个按钮。"""
        def boom():
            raise OSError("stat 挂了")

        monkeypatch.setattr(voicebox_restart, "find_app", boom)

        data = _environment(client, refresh=True)

        assert data["restart_supported"] is False


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


class TestModels:
    def test_lists_dubbing_models_with_status(self, client, vb, fake_upstream):
        """下拉的候选来自后端目录，且只含能配音的模型。

        上游 /models/status 里混着 whisper（转写）和 qwen3（LLM），它们都下好了、
        但**不能拿来配音** —— 混进下拉就是给用户一个点了会报错的选项。
        """
        response = client.get("/api/v1/voicebox/models")

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        names = [item["model_name"] for item in data["items"]]
        assert "whisper-base" not in names, "转写模型不该出现在配音下拉里"
        assert "qwen3-4b" not in names, "LLM 不该出现在配音下拉里"
        assert "qwen-tts-0.6B" in names

        by_name = {item["model_name"]: item for item in data["items"]}
        assert by_name["qwen-tts-0.6B"]["downloaded"] is True
        assert by_name["qwen-tts-0.6B"]["size_mb"] == 2399.5
        assert by_name["qwen-tts-1.7B"]["downloaded"] is False

    def test_engine_and_size_are_both_reported(self, client, vb, fake_upstream):
        """上游要的是 (engine, model_size) 两个参数，不是 model_name —— 页面得拿到
        这两个值才提交得了，所以接口必须把它们一起下发。"""
        items = client.get("/api/v1/voicebox/models").json()["data"]["items"]
        by_name = {item["model_name"]: item for item in items}

        assert by_name["qwen-tts-1.7B"]["engine"] == "qwen"
        assert by_name["qwen-tts-1.7B"]["model_size"] == "1.7B"
        assert by_name["luxtts"]["engine"] == "luxtts"
        # 不分尺寸的引擎给空串：提交时这个字段整个不发（上游对它是正则校验）
        assert by_name["luxtts"]["model_size"] == ""

    def test_unknown_model_name_is_null_not_false(self, client, vb, fake_upstream):
        """上游列表里没有的模型是「不知道」，不是「没下载」。

        上游改个 model_name，对照表就对不上了；这时把每一项都标成「未下载」会
        误导用户去点下载，而真相是版本差异 —— 必须能区分开。
        """
        fake_upstream["models"] = [
            {"model_name": "qwen-tts-v2-1.7B", "downloaded": True, "loaded": True}
        ]

        items = client.get("/api/v1/voicebox/models").json()["data"]["items"]

        assert all(item["downloaded"] is None for item in items)

    def test_upstream_error_is_400_with_readable_message(self, client, vb, fake_upstream):
        fake_upstream["models_error"] = VoiceboxError(
            "unreachable", "连不上 Voicebox 服务：请先打开 Voicebox 桌面端"
        )

        response = client.get("/api/v1/voicebox/models")

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

        output = vb.materials / "dubbing" / "保温杯开场.wav"
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
        index = vb.materials / "dubbing" / ".dubbing-index.json"
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
        assert not (vb.materials / "dubbing" / "失败样本.wav").exists()
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

    def test_unsupported_engine_is_400(self, client, vb, fake_upstream):
        """engine 与 model_size 是**一个组合**：单看各自都合法也要拒。

        (qwen, 1.7B) 和 (kokoro, "") 都在目录里，但 (kokoro, 1.7B) 不是 —— Kokoro
        压根不分尺寸。只查两边的白名单就会把这个组合放过去，撞到上游才报错。
        """
        response = client.post(
            "/api/v1/voicebox/generations",
            json={"text": "hello", "profile_id": "p1", "engine": "kokoro", "model_size": "1.7B"},
        )
        assert response.status_code == 400
        assert "不支持的模型" in response.json()["error"]["message"]

        # 同一个 engine 配它自己的尺寸就该放行
        assert client.post(
            "/api/v1/voicebox/generations",
            json={"text": "hello", "profile_id": "p1", "engine": "kokoro", "model_size": ""},
        ).status_code == 201

    def test_engine_and_size_reach_the_upstream(self, client, vb, fake_upstream):
        """选中的模型要一路透传到上游的 /generate —— 中间任何一层吞掉字段，
        用户选的模型就白选了（会静默退回默认的 qwen 1.7B）。"""
        created = client.post(
            "/api/v1/voicebox/generations",
            json={"text": "hello", "profile_id": "p1", "engine": "luxtts", "model_size": ""},
        )
        assert created.status_code == 201, created.text
        assert created.json()["data"]["engine"] == "luxtts"

        record = _wait_terminal(client, created.json()["data"]["id"])

        assert record["engine"] == "luxtts"
        call = fake_upstream["generate_calls"][-1]
        assert call["engine"] == "luxtts"
        assert call["model_size"] == ""

    def test_catalog_defaults_to_qwen_1_7b(self, client, vb, fake_upstream):
        """不带 engine / model_size 的老请求（比如脚本、旧前端）行为不变。"""
        created = client.post(
            "/api/v1/voicebox/generations", json={"text": "hello", "profile_id": "p1"}
        )
        assert created.status_code == 201, created.text

        _wait_terminal(client, created.json()["data"]["id"])

        call = fake_upstream["generate_calls"][-1]
        assert call["engine"] == "qwen"
        assert call["model_size"] == "1.7B"

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
        return {"materials": vb.materials, "record": finished}

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
        materials = generated["materials"]
        target = materials / "dubbing" / "样本.wav"
        assert target.is_file()

        response = client.delete("/api/v1/voicebox/audios/样本.wav")
        assert response.status_code == 200, response.text
        assert not target.exists()
        assert client.get("/api/v1/voicebox/audios").json()["data"]["total"] == 0

        index_text = (materials / "dubbing" / ".dubbing-index.json").read_text("utf-8")
        assert "样本.wav" not in index_text

    def test_delete_unknown_is_404(self, client, generated):
        assert client.delete("/api/v1/voicebox/audios/没有这个.wav").status_code == 404

    def test_delete_traversal_is_blocked(self, client, generated):
        assert client.delete("/api/v1/voicebox/audios/..%5Csecret.wav").status_code == 400


class TestAudioListingWithoutUpstream:
    def test_user_copied_file_is_listed_but_not_indexed(self, client, vb, fake_upstream):
        """用户自己往 materials/dubbing/ 拷的音频也要列出来（磁盘是唯一真相）。"""
        dubbing = vb.materials / "dubbing"
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
        assert not (vb.materials / "dubbing").exists()

    def test_corrupt_index_falls_back_to_scan(self, client, vb, fake_upstream):
        dubbing = vb.materials / "dubbing"
        dubbing.mkdir(parents=True, exist_ok=True)
        (dubbing / "坏索引.wav").write_bytes(b"RIFF")
        (dubbing / ".dubbing-index.json").write_text("{ 这不是 JSON", encoding="utf-8")

        listing = client.get("/api/v1/voicebox/audios").json()["data"]

        assert listing["total"] == 1
        assert listing["items"][0]["indexed"] is False
