"""重启 Voicebox（services/voicebox_restart.py）的测试。

这是全项目**唯一**会起 / 杀外部进程的模块，所以隔离要求比别处更硬：
`_run` / `_popen` / `_list_processes` / `_terminate` 四个可替换点全部换掉 ——
在开发机上真跑一遍 `osascript quit app "Voicebox"` 会把用户正开着的 Voicebox
关掉。只有一条用例（test_terminate_kills_a_real_process）故意碰真实进程，
它杀的是**本用例自己起的**子进程。

Windows 那一半在 macOS 上跑不了真机，但代码路径能跑：假 `_run` / 假 `_popen`
记录下调用，注册表反查用 tests/fakes.FakeWinreg 喂数据，于是
「taskkill 先温和后强制」「拉起时 env 里带镜像」这些**关键字面**能被钉住。
"""

import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from app.services import voicebox_mirror, voicebox_restart
from tests.fakes import FakeWinreg


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------


class Recorder:
    """记录所有外部动作，一个真进程都不起。"""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.commands: List[List[str]] = []
        self.launches: List[Dict] = []
        self.terminated: List[int] = []
        self.processes: List[Tuple[int, int, str]] = []
        self.delay = delay

    def run(self, argv) -> Tuple[int, str, str]:
        self.commands.append(list(argv))
        if self.delay:
            time.sleep(self.delay)
        return 0, "", ""

    def popen(self, argv, *, env) -> None:
        self.launches.append({"argv": list(argv), "env": dict(env)})
        if self.delay:
            time.sleep(self.delay)

    def terminate(self, pid: int, grace: float = 0.0) -> None:
        self.terminated.append(pid)

    def list_processes(self) -> List[Tuple[int, int, str]]:
        return list(self.processes)


def _server_cmd(app: Path, pid_arg: Optional[int] = None) -> str:
    cmd = f"{app}/Contents/MacOS/voicebox-server --port 17493"
    if pid_arg is not None:
        cmd += f" --parent-pid {pid_arg}"
    return cmd


@pytest.fixture()
def harness(monkeypatch, tmp_path):
    """macOS 路径上的一切外部依赖换成假的。返回 `(recorder, app)`。

    `_macos_candidates` 指向一个**真实存在于 tmp 里**的假 .app：`find_app()` 会
    去 `path.exists()`，让它在真文件系统上跑一趟比整段替换掉更有价值。

    Spotlight 那条分支在这里关掉：`_run` 换成了记录器，`mdfind` 会混进
    `recorder.commands` 把「osascript 排第一」这类顺序断言搅乱。它自己由
    TestFindApp 里三条用例单独覆盖。
    """
    app = tmp_path / "Applications" / "Voicebox.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "voicebox-server").write_text("", encoding="utf-8")

    recorder = Recorder()
    monkeypatch.setattr(voicebox_restart, "platform_key", lambda: "macos")
    monkeypatch.setattr(voicebox_restart, "_macos_candidates", lambda: [app])
    monkeypatch.setattr(voicebox_restart, "_macos_spotlight_candidates", lambda: [])
    monkeypatch.setattr(voicebox_restart, "_run", recorder.run)
    monkeypatch.setattr(voicebox_restart, "_popen", recorder.popen)
    monkeypatch.setattr(voicebox_restart, "_list_processes", recorder.list_processes)
    monkeypatch.setattr(voicebox_restart, "_terminate", recorder.terminate)
    monkeypatch.setattr(voicebox_mirror, "current_value", lambda: None)
    return recorder, app


@pytest.fixture()
def windows_app(monkeypatch, tmp_path):
    """Windows 路径：平台钉成 windows，安装位置给一个真的假 exe。"""
    exe = tmp_path / "Voicebox" / "Voicebox.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(voicebox_restart, "platform_key", lambda: "windows")
    monkeypatch.setattr(voicebox_restart, "find_app", lambda: exe)
    monkeypatch.setattr(voicebox_mirror, "current_value", lambda: None)
    return exe


# --------------------------------------------------------------------------
# 定位安装位置
# --------------------------------------------------------------------------


class TestFindApp:
    def test_finds_the_first_existing_candidate(self, monkeypatch, tmp_path):
        missing = tmp_path / "missing" / "Voicebox.app"
        present = tmp_path / "present" / "Voicebox.app"
        present.mkdir(parents=True)
        monkeypatch.setattr(voicebox_restart, "_macos_candidates", lambda: [missing, present])
        # Spotlight 也返回一条更像真的，但它排在标准位置之后，不该被选中
        monkeypatch.setattr(
            voicebox_restart, "_macos_spotlight_candidates", lambda: [tmp_path / "spot" / "V.app"]
        )

        assert voicebox_restart.find_app() == present

    def test_not_found_returns_none(self, monkeypatch, tmp_path):
        """**不做缓存**：用户可能刚装上 Voicebox，缓存住「没找到」会让页面一直
        说「没找到安装位置」。"""
        monkeypatch.setattr(
            voicebox_restart, "_macos_candidates", lambda: [tmp_path / "nope" / "Voicebox.app"]
        )
        monkeypatch.setattr(voicebox_restart, "_macos_spotlight_candidates", lambda: [])

        assert voicebox_restart.find_app() is None

        # 装上之后再问一次，就该找得到
        app = tmp_path / "late" / "Voicebox.app"
        app.mkdir(parents=True)
        monkeypatch.setattr(voicebox_restart, "_macos_candidates", lambda: [app])
        assert voicebox_restart.find_app() == app

    def test_spotlight_is_used_when_standard_paths_miss(self, monkeypatch, tmp_path):
        app = tmp_path / "外置卷" / "Voicebox.app"
        app.mkdir(parents=True)
        monkeypatch.setattr(
            voicebox_restart, "_macos_candidates", lambda: [tmp_path / "nope" / "Voicebox.app"]
        )
        monkeypatch.setattr(voicebox_restart, "_macos_spotlight_candidates", lambda: [app])

        assert voicebox_restart.find_app() == app

    def test_searched_paths_lists_everything_it_looked_at(self, monkeypatch, tmp_path):
        """报错文案要**列全**查过的地方（subtitle_env 的教训：只报最后一条，
        用户会以为工具没找他装的那个位置，然后自己重装一遍）。"""
        monkeypatch.setattr(
            voicebox_restart,
            "_macos_candidates",
            lambda: [tmp_path / "a" / "Voicebox.app", tmp_path / "b" / "Voicebox.app"],
        )

        listed = voicebox_restart.searched_paths()

        assert str(tmp_path / "a" / "Voicebox.app") in listed
        assert str(tmp_path / "b" / "Voicebox.app") in listed
        assert any("Spotlight" in text for text in listed)

    def test_windows_searched_paths_mention_the_registry(self, monkeypatch):
        monkeypatch.setattr(voicebox_restart, "platform_key", lambda: "windows")
        monkeypatch.setattr(voicebox_restart, "_windows_candidates", lambda: [])
        monkeypatch.setattr(voicebox_restart, "_macos_spotlight_candidates", lambda: [])

        assert any("注册表" in text for text in voicebox_restart.searched_paths())

    def test_windows_registry_reverse_lookup_finds_custom_install(self, monkeypatch, tmp_path):
        """Tauri 的 NSIS / MSI 都会写卸载登记表 —— 用户装在非默认目录时，
        这是唯一可靠的线索。"""
        exe = tmp_path / "Custom" / "Voicebox.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")

        registry = FakeWinreg()
        uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
        with registry.CreateKeyEx(registry.HKEY_LOCAL_MACHINE, uninstall + r"\Voicebox") as key:
            registry.SetValueEx(key, "DisplayName", 0, registry.REG_SZ, "Voicebox")
            registry.SetValueEx(key, "InstallLocation", 0, registry.REG_SZ, str(exe.parent))
        # 别的软件不该被误认
        with registry.CreateKeyEx(registry.HKEY_LOCAL_MACHINE, uninstall + r"\Other") as key:
            registry.SetValueEx(key, "DisplayName", 0, registry.REG_SZ, "SomeOtherApp")
            registry.SetValueEx(key, "InstallLocation", 0, registry.REG_SZ, str(tmp_path))

        monkeypatch.setattr(voicebox_restart, "_winreg", registry)
        monkeypatch.setattr(voicebox_restart, "platform_key", lambda: "windows")
        # 标准位置全部落空（环境变量指到不存在的目录），只剩注册表反查这一条
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-local"))
        monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "no-pf"))
        monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "no-pf86"))

        assert voicebox_restart.find_app() == exe

    def test_windows_registry_display_icon_fallback(self, monkeypatch, tmp_path):
        """有的包只写 DisplayIcon（形如 `C:\\...\\Voicebox.exe,0`），索引后缀要去掉。"""
        exe = tmp_path / "IconOnly" / "Voicebox.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")

        registry = FakeWinreg()
        uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
        with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, uninstall + r"\Voicebox") as key:
            registry.SetValueEx(key, "DisplayName", 0, registry.REG_SZ, "Voicebox")
            registry.SetValueEx(key, "DisplayIcon", 0, registry.REG_SZ, f"{exe},0")

        monkeypatch.setattr(voicebox_restart, "_winreg", registry)
        monkeypatch.setattr(voicebox_restart, "platform_key", lambda: "windows")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-local"))
        monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "no-pf"))
        monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "no-pf86"))

        assert voicebox_restart.find_app() == exe


# --------------------------------------------------------------------------
# macOS 重启
# --------------------------------------------------------------------------


class TestMacosRestart:
    def test_quit_then_cleanup_then_launch(self, harness):
        """顺序不能变：先让启动器退出，再清孤儿 server，最后才拉起。

        反过来的话，新启动器会复用那个还在跑的旧服务，新设的 HF_ENDPOINT 对它
        无效 —— 用户看到的就是「重启了但没生效」。
        """
        recorder, app = harness
        launcher_pid = 4242
        recorder.processes = [
            (launcher_pid, 1, f"{app}/Contents/MacOS/voicebox"),
            (4300, launcher_pid, _server_cmd(app, launcher_pid)),
            (4301, 1, f"{app}/Contents/MacOS/voicebox-server --port 17493"),
        ]

        result = voicebox_restart.restart()

        # 中间不该夹着 pkill 之类「图省事」的补刀：启动器由 osascript 退出就够了，
        # 但**必须发生在拉起之前**，否则新启动器会复用还在跑的旧服务
        assert recorder.commands == [
            ["osascript", "-e", 'quit app "Voicebox"'],
            ["open", "-a", str(app)],
        ]
        assert recorder.terminated == [4300, 4301], "同 .app 内的残留 server 都要清掉"
        assert launcher_pid not in recorder.terminated, "启动器自己由 osascript 退出，不该再补一刀"
        assert result.app_path == str(app)
        assert result.killed_pids == [4300, 4301]
        assert recorder.launches == [], "macOS 走 open，不用 _popen（open 没有传 env 的参数）"

    def test_server_from_source_build_is_not_killed(self, harness, tmp_path):
        """用户从源码跑的 server 不能被误杀 —— 它跟我们的安装没关系。"""
        recorder, app = harness
        recorder.processes = [
            (10, 1, f"{app}/Contents/MacOS/voicebox"),
            (11, 1, f"{tmp_path}/dev/voicebox/voicebox-server --port 17499"),
        ]

        voicebox_restart.restart()

        assert recorder.terminated == []
        assert 11 not in recorder.terminated

    def test_parent_pid_match_is_the_fallback(self, harness, tmp_path):
        """运行中的实例不是我们找到的那份安装（两份都装着）时，靠 --parent-pid 认。"""
        recorder, app = harness
        other_app = tmp_path / "Other" / "Voicebox.app"
        recorder.processes = [
            (20, 1, f"{app}/Contents/MacOS/voicebox"),
            # 这个 server 的路径不在任何一个候选 .app 里，但认自己是 pid 20 的孩子
            (21, 20, f"{other_app}/Contents/MacOS/voicebox-server --parent-pid 20"),
        ]

        result = voicebox_restart.restart()

        assert recorder.terminated == [21]
        assert result.killed_pids == [21]

    def test_unrelated_process_is_left_alone(self, harness):
        recorder, app = harness
        recorder.processes = [
            (30, 1, f"{app}/Contents/MacOS/voicebox"),
            (31, 1, "/usr/bin/voicebox-server-helper"),  # 名字像但不是我们的
            (32, 1, "/usr/bin/其他什么进程"),
        ]

        voicebox_restart.restart()

        assert recorder.terminated == []

    def test_missing_app_raises_and_lists_where_it_looked(self, harness, monkeypatch, tmp_path):
        """「重启」了却没重启，比诚实地报错更糟 —— 所以找不到就不猜。"""
        recorder, _app = harness
        monkeypatch.setattr(
            voicebox_restart,
            "_macos_candidates",
            lambda: [tmp_path / "nope" / "Voicebox.app"],
        )

        with pytest.raises(voicebox_restart.VoiceboxNotFoundError) as excinfo:
            voicebox_restart.restart()

        message = excinfo.value.user_message
        assert str(tmp_path / "nope" / "Voicebox.app") in message
        assert "Spotlight" in message
        assert recorder.commands == [], "没找到就别去动用户的进程"

    def test_linux_is_unsupported(self, harness, monkeypatch, tmp_path):
        """Linux 上桌面应用怎么拿到环境变量取决于发行版 —— 没有可靠做法就如实说不支持。"""
        app = tmp_path / "Voicebox.app"
        app.mkdir()
        monkeypatch.setattr(voicebox_restart, "platform_key", lambda: "linux")
        monkeypatch.setattr(voicebox_restart, "find_app", lambda: app)

        with pytest.raises(voicebox_restart.RestartError) as excinfo:
            voicebox_restart.restart()

        assert "手动" in excinfo.value.user_message

    def test_orchestration_has_no_hidden_wait(self, harness, monkeypatch):
        """整条路径必须是毫秒级：**设计上不在后端等就绪**。

        真要等就绪（实测约 30 秒）必然撞上前端 15 秒的 fetch 超时，用户会以为
        重启失败。就绪交给前端轮询。有人往这里加 `sleep` 时，这条会先失败。
        """
        recorder, app = harness
        recorder.delay = 0.01  # 每条外部命令「耗时」10 毫秒
        recorder.processes = [(1, 1, _server_cmd(app, 1))]

        started = time.monotonic()
        voicebox_restart.restart()
        elapsed = time.monotonic() - started

        assert elapsed < 0.5, f"重启编排里有多余的等待：{elapsed:.2f}s"

    def test_single_step_cannot_eat_the_frontend_timeout(self):
        """前端 fetch 15 秒超时，而重启是同步接口。

        真机最坏路径（等 Voicebox 真的退出、真的变成孤儿）在测试里复现不了，
        所以这条钉的是**常量之间的关系**：单步超时加上清理残留的宽限期，必须给
        其余步骤留出余量。有人调大 `_RUN_TIMEOUT_SECONDS` 或 `_KILL_GRACE_SECONDS`
        时它会先失败。
        """
        budget = (
            voicebox_restart._RUN_TIMEOUT_SECONDS
            + 2 * voicebox_restart._KILL_GRACE_SECONDS
        )

        assert budget < 15.0, f"重启预算 {budget}s 会撞上前端 15s 超时"


# --------------------------------------------------------------------------
# Windows 重启
# --------------------------------------------------------------------------


class TestWindowsRestart:
    def test_taskkill_gentle_before_forced(self, harness, windows_app, monkeypatch):
        """先温和（让 Tauri 自己收 sidecar），再按映像名强制补刀。

        补刀按映像名的理由：`/T` 只覆盖进程树，对已孤儿化的 server 无效，而
        Voicebox 有「保持后台服务运行」的偏好会让它独立存活。
        """
        recorder, _app = harness
        monkeypatch.setattr(voicebox_restart, "_run", recorder.run)

        voicebox_restart.restart()

        kills = [argv for argv in recorder.commands if argv[0] == "taskkill"]
        assert kills == [
            ["taskkill", "/IM", "Voicebox.exe", "/T"],
            ["taskkill", "/F", "/IM", "Voicebox.exe"],
            ["taskkill", "/F", "/IM", "voicebox-server.exe"],
        ]

    def test_launch_explicitly_passes_the_mirror_env(self, harness, windows_app, monkeypatch):
        """**这是 Windows 上最关键的一条**：镜像写进了注册表，但我们自己的环境块
        在启动时就固定了，子进程继承的是旧环境块 —— 不显式带上就白设了。"""
        recorder, _app = harness
        monkeypatch.setattr(voicebox_restart, "_popen", recorder.popen)
        monkeypatch.setattr(
            voicebox_mirror, "current_value", lambda: voicebox_mirror.HF_MIRROR_URL
        )

        voicebox_restart.restart()

        assert len(recorder.launches) == 1
        launched = recorder.launches[0]
        assert launched["argv"] == [str(windows_app)]
        assert launched["env"]["HF_ENDPOINT"] == voicebox_mirror.HF_MIRROR_URL

    def test_launch_drops_a_stale_mirror_from_our_own_env(self, harness, windows_app, monkeypatch):
        """用户点了「清除镜像」再重启：新进程不能从我们的环境块里继承到旧值，
        否则「清除」等于没清。"""
        recorder, _app = harness
        monkeypatch.setattr(voicebox_restart, "_popen", recorder.popen)
        monkeypatch.setenv("HF_ENDPOINT", "https://hf-api.gitee.com")  # 我们自己的旧值
        monkeypatch.setattr(voicebox_mirror, "current_value", lambda: None)

        voicebox_restart.restart()

        assert "HF_ENDPOINT" not in recorder.launches[0]["env"]

    def test_restart_starts_the_exe_directly(self, harness, windows_app, monkeypatch):
        """Windows 上没有 macOS 那样的会话级环境注入兜底，必须自己带 env 起 exe。"""
        recorder, _app = harness
        monkeypatch.setattr(voicebox_restart, "_popen", recorder.popen)

        result = voicebox_restart.restart()

        assert result.app_path == str(windows_app)
        assert recorder.launches[0]["argv"] == [str(windows_app)]


# --------------------------------------------------------------------------
# 终止进程（这条碰真实进程，但杀的是本用例自己起的）
# --------------------------------------------------------------------------


class TestTerminate:
    def test_kills_a_real_process_we_started(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # noqa: S603
        try:
            assert voicebox_restart._is_alive(child.pid) is True

            voicebox_restart._terminate(child.pid, grace=1.0)

            # 僵尸进程在被 wait 回收前 os.kill(pid, 0) 仍然成功，所以这里靠
            # wait 拿退出码：SIGTERM(-15) 或等宽限期后的 SIGKILL(-9) 都算杀掉了
            assert child.wait(timeout=5) in (-15, -9)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_vanished_process_is_not_an_error(self):
        """进程刚好在这时候自己退了 —— 幂等，不该抛。"""
        child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
        child.wait(timeout=5)

        voicebox_restart._terminate(child.pid, grace=0.1)

    def test_is_alive_sees_our_own_process(self):
        import os

        assert voicebox_restart._is_alive(os.getpid()) is True
