"""VideoCaptioner 探测与安装指引（subtitle_env.py）测试。

探测是会起子进程、且有副作用（被调包导入时会在 HOME 建配置目录）的操作，
所以这里的原则是：**子进程一律用 monkeypatch 换掉**，只验证
「候选顺序、路径拼接、缓存、解析、指引文案」这些我们自己的逻辑。
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import subtitle_env
from app.services.subtitle_env import (
    VcInstall,
    detect,
    install_hints,
    platform_key,
    probe_environment,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _isolate_detection(monkeypatch):
    """每个用例都清缓存，并默认把「外部环境」全部挡掉：

    - 探测缓存清空（它是模块级状态，不清会串用例）；
    - PATH 查找返回 None（不去摸开发机真实环境）；
    - 后端解释器就地判断固定为 False（理由同上）；
    - VC 根目录指向一个不存在的路径（venv 候选自然落空）。
    """
    reset_cache()
    monkeypatch.setattr(subtitle_env, "find_tool", lambda *a, **k: None)
    monkeypatch.setattr(subtitle_env, "_module_available_locally", lambda: False)
    monkeypatch.setattr(settings, "SUBTITLE_VC_PYTHON", "")
    monkeypatch.setattr(settings, "SUBTITLE_VC_ROOT", "/nonexistent/vc-root")
    yield
    reset_cache()


class TestCandidates:
    """候选调用方式的顺序与跨平台路径拼接。"""

    def test_explicit_override_comes_first(self, monkeypatch):
        """配置里显式指定的解释器优先级最高，且「解释器」「脚本」两种形态都试。"""
        monkeypatch.setattr(settings, "SUBTITLE_VC_PYTHON", "/opt/py311/bin/python")
        candidates = subtitle_env._candidates()
        assert candidates[0] == (
            ["/opt/py311/bin/python", "-m", "videocaptioner"],
            "override",
            settings.SUBTITLE_VC_ROOT,
        )
        assert candidates[1] == (["/opt/py311/bin/python"], "override-script",
                                 settings.SUBTITLE_VC_ROOT)

    def test_venv_paths_on_posix(self):
        """POSIX：venv 里是 bin/python 与 bin/videocaptioner。"""
        candidates = subtitle_env._candidates()
        root = settings.SUBTITLE_VC_ROOT
        assert ([f"{root}/.venv/bin/python", "-m", "videocaptioner"], "venv-python",
                root) in candidates
        assert ([f"{root}/.venv/bin/videocaptioner"], "venv-script",
                root) in candidates

    def test_venv_paths_on_windows(self, monkeypatch):
        """Windows：venv 布局变成 Scripts/python.exe 与 Scripts/videocaptioner.exe。

        只把模块里的 os 换成「自称 nt」的替身来做纯字符串拼接验证 —— 真的把
        os.name 改掉会让 pathlib 在本机上实例化 WindowsPath 而直接报错。
        """
        monkeypatch.setattr(subtitle_env, "os", SimpleNamespace(name="nt"))
        candidates = subtitle_env._candidates()
        kinds = {kind for _launcher, kind, _root in candidates}
        assert "venv-python" in kinds
        assert "venv-script" in kinds
        launchers = [launcher[0] for launcher, kind, _r in candidates
                     if kind == "venv-python"]
        assert launchers[0].endswith(".venv/Scripts/python.exe")
        scripts = [launcher[0] for launcher, kind, _r in candidates
                   if kind == "venv-script"]
        assert scripts[0].endswith(".venv/Scripts/videocaptioner.exe")

    def test_backend_python_candidate_when_module_available(self, monkeypatch):
        """后端环境里能 import 到包时，补一条 sys.executable -m 候选。"""
        monkeypatch.setattr(subtitle_env, "_module_available_locally", lambda: True)
        candidates = subtitle_env._candidates()
        assert ([sys.executable, "-m", "videocaptioner"], "backend-python",
                "") in candidates

    def test_path_candidate_prefers_sibling_python(self, monkeypatch, tmp_path):
        """PATH 上只有脚本时，顺着它找同目录的解释器（-m 比直接调脚本更稳）。"""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        script = bin_dir / "videocaptioner"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        sibling = bin_dir / "python3"
        sibling.write_text("#!/bin/sh\n", encoding="utf-8")

        def fake_find_tool(name):
            return str(script) if name == "videocaptioner" else None

        monkeypatch.setattr(subtitle_env, "find_tool", fake_find_tool)
        candidates = subtitle_env._candidates()
        assert ([str(sibling), "-m", "videocaptioner"], "path", "") in candidates
        assert ([str(script)], "path-script", "") in candidates


class TestProbe:
    """单次探测：命令形态、解析、各种失败都不抛异常。"""

    def _patch_run(self, monkeypatch, recorder, *, returncode=0, stdout=""):
        def fake_run(argv, **kwargs):
            recorder.append(argv)
            return SimpleNamespace(returncode=returncode, stdout=stdout)

        monkeypatch.setattr(subtitle_env.subprocess, "run", fake_run)

    def test_module_form_probes_interpreter_with_dash_c(self, monkeypatch):
        """[python, -m, videocaptioner] 形态：探测问的是解释器（-c 小脚本）。"""
        calls = []
        payload = {"version": "1.4.2", "config_file": "/cfg/config.toml",
                   "python_version": "3.11.9", "executable": "/py"}
        self._patch_run(monkeypatch, calls, stdout=json.dumps(payload))

        result = subtitle_env._probe(["/py", "-m", "videocaptioner"])
        assert calls[0][0] == "/py"
        assert calls[0][1] == "-c"
        # 探测绝不能把 -m videocaptioner 带上 —— 那是转写时才用的
        assert "-m" not in calls[0][:2]
        assert result == payload

    def test_script_form_uses_version_flag(self, monkeypatch):
        """[videocaptioner] 形态：拿不到解释器，只能问它自己的 --version。"""
        calls = []
        self._patch_run(monkeypatch, calls, stdout="videocaptioner 1.4.2\n")

        result = subtitle_env._probe(["/usr/local/bin/videocaptioner"])
        assert calls[0] == ["/usr/local/bin/videocaptioner", "--version"]
        assert result == {"version": "1.4.2", "config_file": ""}

    def test_probe_json_takes_last_json_line(self):
        """包导入期间可能往 stdout 打警告：真正的结果在最后一行。"""
        stdout = "UserWarning: something\n{\"version\": \"1.0.0\"}\n"
        assert subtitle_env._parse_probe_json(stdout) == {"version": "1.0.0"}

    def test_nonzero_exit_returns_none(self, monkeypatch):
        calls = []
        self._patch_run(monkeypatch, calls, returncode=3, stdout="")
        assert subtitle_env._probe(["/py", "-m", "videocaptioner"]) is None

    def test_oserror_returns_none(self, monkeypatch):
        def fake_run(argv, **kwargs):
            raise OSError("没有那个文件")

        monkeypatch.setattr(subtitle_env.subprocess, "run", fake_run)
        assert subtitle_env._probe(["/py", "-m", "videocaptioner"]) is None

    def test_timeout_returns_none(self, monkeypatch):
        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

        monkeypatch.setattr(subtitle_env.subprocess, "run", fake_run)
        assert subtitle_env._probe(["/py", "-m", "videocaptioner"]) is None

    def test_unparseable_output_returns_none(self, monkeypatch):
        calls = []
        self._patch_run(monkeypatch, calls, stdout="完全是别的输出")
        assert subtitle_env._probe(["/py", "-m", "videocaptioner"]) is None


class TestDetect:
    """整体探测：候选逐个试、失败不抛异常、结果带缓存。"""

    def test_all_candidates_fail_returns_not_installed(self, monkeypatch):
        """一个候选都不通：返回 installed=False 与可读原因，绝不抛异常。"""
        monkeypatch.setattr(subtitle_env, "_probe", lambda launcher: None)
        install = detect()
        assert install.installed is False
        assert install.ready is False
        assert "没有找到可用的 VideoCaptioner" in install.detail
        # kind/root 记录最后一次尝试，方便排查「它到底找了哪儿」
        assert install.kind == "venv-script"
        assert install.root == settings.SUBTITLE_VC_ROOT

    def test_first_working_candidate_wins(self, monkeypatch, tmp_path):
        """按顺序试，第一个通的候选就是结果（后面的不再试）。"""
        monkeypatch.setattr(subtitle_env, "_candidates", lambda: [
            (["/a/python", "-m", "videocaptioner"], "venv-python", "/a"),
            (["/b/videocaptioner"], "path-script", ""),
        ])
        tried = []

        def fake_probe(launcher):
            tried.append(launcher[0])
            if launcher[0] == "/b/videocaptioner":
                return {"version": "1.4.2", "config_file": str(tmp_path / "c.toml")}
            return None

        monkeypatch.setattr(subtitle_env, "_probe", fake_probe)
        install = detect()
        assert tried == ["/a/python", "/b/videocaptioner"]
        assert install.installed is True
        assert install.kind == "path-script"
        assert install.version == "1.4.2"
        assert install.launcher == ("/b/videocaptioner",)
        assert install.config_exists is False  # 配置文件还没建

    def test_config_exists_flag(self, monkeypatch, tmp_path):
        """配置文件真实存在时 config_exists 为 True（用户大概率配过 key）。"""
        config = tmp_path / "config.toml"
        config.write_text("[asr]\n", encoding="utf-8")
        monkeypatch.setattr(subtitle_env, "_candidates", lambda: [
            (["/a/python", "-m", "videocaptioner"], "venv-python", "/a"),
        ])
        monkeypatch.setattr(
            subtitle_env, "_probe",
            lambda launcher: {"version": "1.0.0", "config_file": str(config)},
        )
        assert detect().config_exists is True

    def test_cache_avoids_reprobe(self, monkeypatch):
        """缓存命中时不再起子进程；force=True 绕过缓存。"""
        monkeypatch.setattr(subtitle_env, "_candidates", lambda: [
            (["/a/python", "-m", "videocaptioner"], "venv-python", "/a"),
        ])
        calls = []

        def fake_probe(launcher):
            calls.append(launcher)
            return {"version": "1.0.0"}

        monkeypatch.setattr(subtitle_env, "_probe", fake_probe)
        detect()
        detect()
        assert len(calls) == 1  # 第二次读的缓存

        detect(force=True)
        assert len(calls) == 2

    def test_cache_expires_after_ttl(self, monkeypatch):
        """缓存超过 SUBTITLE_DETECT_CACHE_SECONDS 后重新探测。"""
        monkeypatch.setattr(subtitle_env, "_candidates", lambda: [
            (["/a/python", "-m", "videocaptioner"], "venv-python", "/a"),
        ])
        calls = []
        monkeypatch.setattr(
            subtitle_env, "_probe",
            lambda launcher: calls.append(launcher) or {"version": "1.0.0"},
        )
        monkeypatch.setattr(settings, "SUBTITLE_DETECT_CACHE_SECONDS", 30)

        clock = {"now": 1000.0}
        monkeypatch.setattr(subtitle_env.time, "monotonic", lambda: clock["now"])

        detect()
        clock["now"] += 10  # TTL 内
        detect()
        assert len(calls) == 1

        clock["now"] += 25  # 超过 TTL
        detect()
        assert len(calls) == 2


class TestInstallHints:
    """安装指引按平台出文案（只出当前平台那一份）。"""

    def test_windows_hints(self, monkeypatch):
        monkeypatch.setattr(subtitle_env, "platform_key", lambda: "windows")
        hints = install_hints()
        commands = [h["command"] for h in hints]
        assert "py -3.11 -m pip install videocaptioner" in commands
        assert "winget install Gyan.FFmpeg" in commands
        # 最后一条总是「装好后回来点重新检测」
        assert "重新检测" in hints[-1]["note"]

    def test_macos_hints(self, monkeypatch):
        monkeypatch.setattr(subtitle_env, "platform_key", lambda: "macos")
        hints = install_hints()
        commands = [h["command"] for h in hints]
        assert "python3 -m pip install videocaptioner" in commands
        assert "brew install ffmpeg" in commands
        assert any("run.sh" in c for c in commands)

    def test_linux_hints(self, monkeypatch):
        monkeypatch.setattr(subtitle_env, "platform_key", lambda: "linux")
        hints = install_hints()
        commands = [h["command"] for h in hints]
        assert "sudo apt install ffmpeg" in commands

    def test_platform_key_on_this_mac(self):
        """开发机是 macOS，platform_key 应给出 macos。"""
        if sys.platform == "darwin" and os.name != "nt":
            assert platform_key() == "macos"


class TestProbeEnvironment:
    """接口形态的环境自检结果。"""

    def test_payload_shape(self, monkeypatch):
        monkeypatch.setattr(subtitle_env, "_candidates", lambda: [])
        payload = probe_environment()
        assert payload["installed"] is False
        assert payload["platform"] in ("macos", "windows", "linux")
        assert payload["platform_label"]
        assert payload["materials_dir"]
        assert payload["default_input_dir"].endswith("source")
        assert payload["default_output_dir"].endswith("subtitle")
        assert isinstance(payload["install_hints"], list)
        assert payload["install_hints"]  # 没装时必须有指引
