"""用户级环境变量读写（services/user_env.py）的测试。

**硬约束：这两条路径都有真实副作用。** macOS 那份会往真的
`~/Library/LaunchAgents/` 写 plist 并调 `launchctl setenv`（会改开发机的图形
会话环境），Windows 那份会改真的注册表。所以四个可替换点
（`_run` / `_LAUNCH_AGENTS_DIR` / `_winreg` / `_ctypes`）一个都不能漏 —— 每个
用例都要经 `_isolate` 夹具，它把这四处全部换掉。

Windows 那一半在 macOS 开发机上跑不了真机，但**代码路径能跑**：`FakeWinreg`
按 CPython 文档实现了 `user_env` 用到的那几个接口，于是同一条 `_set_windows()`
会被真的执行一遍。这是对「Windows 未能实机验证」能给出的最强回应。

假 launchctl 也放在 tests/fakes.py —— 接口测试（test_voicebox_api.py）同样要用
它，两份实现迟早会跑偏。
"""

from pathlib import Path

import pytest

from app.services import user_env
from tests.fakes import FakeCtypes, FakeLaunchctl, FakeWinreg


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture()
def macos(monkeypatch, tmp_path):
    """把平台钉成 macOS，并拦掉全部真实副作用。返回假的 launchctl 与落盘目录。"""
    launchctl = FakeLaunchctl()
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    monkeypatch.setattr(user_env, "platform_key", lambda: "macos")
    monkeypatch.setattr(user_env, "_run", launchctl)
    monkeypatch.setattr(user_env, "_LAUNCH_AGENTS_DIR", agents_dir)
    return launchctl, agents_dir


@pytest.fixture()
def windows(monkeypatch):
    """把平台钉成 Windows，并把注册表与 ctypes 换成假的。"""
    registry = FakeWinreg()
    ctypes = FakeCtypes()
    monkeypatch.setattr(user_env, "platform_key", lambda: "windows")
    monkeypatch.setattr(user_env, "_winreg", registry)
    monkeypatch.setattr(user_env, "_ctypes", ctypes)
    return registry, ctypes


@pytest.fixture()
def linux(monkeypatch):
    monkeypatch.setattr(user_env, "platform_key", lambda: "linux")


def _plist(agents_dir: Path) -> Path:
    return agents_dir / f"{user_env.LAUNCH_AGENT_LABEL}.plist"


# --------------------------------------------------------------------------
# macOS
# --------------------------------------------------------------------------


class TestMacos:
    def test_set_writes_plist_and_loads_it(self, macos):
        """一条成功路径要同时做三件事：落 plist、装载、读回验证。"""
        launchctl, agents_dir = macos

        user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")

        plist = _plist(agents_dir)
        assert plist.is_file(), "plist 必须落在 LaunchAgents 里（重启后才还在）"
        text = plist.read_text("utf-8")
        assert "launchctl" in text and "setenv" in text
        assert "HF_ENDPOINT" in text and "https://hf-api.gitee.com" in text
        assert user_env.LAUNCH_AGENT_LABEL in text
        # RunAtLoad：每次登录都执行一次，否则注销后就没了
        assert "<key>RunAtLoad</key>" in text

        # 顺序：先确认在图形会话里 → 卸载旧的（幂等）→ 装载新的 → 读回验证
        assert launchctl.verbs() == ["print", "bootout", "bootstrap", "getenv"]
        # 装载后立刻生效：值写进了图形会话，桌面应用下次启动就能拿到
        assert launchctl.env["HF_ENDPOINT"] == "https://hf-api.gitee.com"

    def test_bootout_failure_is_tolerated(self, macos, monkeypatch):
        """第一次设置时 label 根本没装载过，bootout 必然非 0 —— 这是预期，不是错误。

        假 launchctl 这里让 bootout 返回非 0，用例应照常成功。
        """
        launchctl, agents_dir = macos

        def run(argv):
            if len(argv) > 1 and argv[1] == "bootout":
                launchctl.commands.append(list(argv))
                return 5, "", "Boot-out failed: 5: Input/output error"
            return launchctl(argv)

        monkeypatch.setattr(user_env, "_run", run)

        user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")

        assert _plist(agents_dir).is_file()
        assert launchctl.env["HF_ENDPOINT"] == "https://hf-api.gitee.com"

    def test_bootstrap_failure_falls_back_to_kickstart(self, macos):
        """已装载时 bootstrap 也会失败（本机实测），kickstart 重跑一次兜底。"""
        launchctl, agents_dir = macos
        launchctl.bootstrap_rc = 5
        launchctl.kickstart_rc = 0
        # kickstart 重跑的 job 里带着旧值，这里让它「生效」以便走到成功分支
        launchctl.env["HF_ENDPOINT"] = "https://hf-api.gitee.com"

        user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")

        assert "kickstart" in launchctl.verbs()

    def test_both_bootstrap_and_kickstart_fail(self, macos):
        launchctl, _agents_dir = macos
        launchctl.bootstrap_rc = 5
        launchctl.kickstart_rc = 3

        with pytest.raises(user_env.UserEnvError) as excinfo:
            user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")

        assert "LaunchAgent" in excinfo.value.user_message
        assert "bootstrap" in excinfo.value.detail

    def test_readback_mismatch_is_an_error_not_silent_success(self, macos, monkeypatch):
        """写盘 + 装载都「成功」了，但会话里读不回来 —— 必须报错。

        报成功的代价是用户半小时后撞上「模型还是下不动」，那时更难查。
        """
        launchctl, agents_dir = macos
        # 装载「成功」了，但 launchd 跑那条 setenv 时没把值放进会话
        launchctl.never_applies = True
        monkeypatch.setattr(user_env, "_VERIFY_INTERVAL_SECONDS", 0.0)

        with pytest.raises(user_env.UserEnvError) as excinfo:
            user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")

        assert "未生效" in excinfo.value.user_message
        # detail 里带上 plist 路径：排查时第一件事就是去看那个文件
        assert str(_plist(agents_dir)) in excinfo.value.detail

    def test_deferred_launchd_execution_is_waited_for(self, macos, monkeypatch):
        """**真机回归**：`launchctl bootstrap` 是异步的 —— 它把 job 交给 launchd，
        launchd 之后再执行那条 `launchctl setenv`。

        所以 bootstrap 刚返回时读 `getenv` 可能还是旧值。这条在真机上必然发生：
        先「清除镜像」（会话里变空）再「设置镜像」，紧跟的读回拿到空的，操作就
        报 500 失败 —— 但实际上它马上就生效了。
        """
        launchctl, agents_dir = macos
        # launchd 要过 50ms 才真的执行那条 setenv，而读回验证就在这之后紧接着发生
        launchctl.apply_delay_seconds = 0.05

        user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")

        assert launchctl.env["HF_ENDPOINT"] == "https://hf-api.gitee.com"
        assert _plist(agents_dir).is_file()

    def test_clear_then_set_again_works(self, macos, monkeypatch):
        """清除后再设置要能成功（真机上就是这条先炸的），且值确实回来了。"""
        launchctl, _agents_dir = macos
        launchctl.apply_delay_seconds = 0.05

        user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")
        user_env.clear_value("HF_ENDPOINT")
        assert user_env.read_state("HF_ENDPOINT").value is None

        user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")

        assert user_env.read_state("HF_ENDPOINT").value == "https://hf-api.gitee.com"

    def test_verify_wait_is_bounded_and_short(self):
        """重试总时长必须小 —— 设镜像也是同步接口，前端 fetch 15 秒超时。

        它挡住的是「有人把等待调成几十秒」这种改动。
        """
        budget = user_env._VERIFY_ATTEMPTS * user_env._VERIFY_INTERVAL_SECONDS

        assert budget <= 2.0, f"读回验证最多等 {budget}s，太久了"

    def test_not_in_gui_session_gives_a_copyable_command(self, macos):
        """后端由 launchd / SSH / sudo 启动时注入不了，文案要能让用户照抄。"""
        launchctl, agents_dir = macos
        launchctl.in_gui_session = False

        with pytest.raises(user_env.UserEnvError) as excinfo:
            user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")

        message = excinfo.value.user_message
        assert "launchctl setenv HF_ENDPOINT https://hf-api.gitee.com" in message
        # 连会话都没进，就别留下一个装了也没用的 plist
        assert not _plist(agents_dir).exists()

    def test_clear_removes_plist_and_is_idempotent(self, macos):
        launchctl, agents_dir = macos
        user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")

        user_env.clear_value("HF_ENDPOINT")
        assert not _plist(agents_dir).exists()
        assert "HF_ENDPOINT" not in launchctl.env

        # 再来一次：本来就没有，不该报错
        user_env.clear_value("HF_ENDPOINT")

    def test_clear_raises_when_value_survives(self, macos, monkeypatch):
        """值可能是别的 LaunchAgent / 用户自己 setenv 留下的 —— 清不掉要说出来，
        不能让页面显示成「已清除」而实际还生效。"""
        launchctl, _agents_dir = macos
        monkeypatch.setattr(
            user_env, "_run", lambda argv: (0, "https://someone-elses\n", "")
        )

        with pytest.raises(user_env.UserEnvError) as excinfo:
            user_env.clear_value("HF_ENDPOINT")

        assert "仍能读到" in excinfo.value.user_message

    def test_state_reports_value_and_persistence(self, macos):
        """`persistent` 单独看 plist 在不在：用户自己 launchctl setenv 的值
        「设了但重启就没了」，这种状态必须如实报出来。"""
        launchctl, agents_dir = macos

        # 只有会话里的值，没有 plist —— 不持久
        launchctl.env["HF_ENDPOINT"] = "https://hf-api.gitee.com"
        state = user_env.read_state("HF_ENDPOINT")
        assert state.value == "https://hf-api.gitee.com"
        assert state.persistent is False

        # 走一次正规设置：plist 有了，就持久了
        user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")
        state = user_env.read_state("HF_ENDPOINT")
        assert state.persistent is True

    def test_unset_reads_as_none(self, macos):
        """未设置与「显式设成空」必须分开：前者是 None。"""
        assert user_env.read_state("HF_ENDPOINT").value is None

    def test_getenv_failure_is_an_error(self, macos, monkeypatch):
        """rc≠0 是 launchctl 本身不可用 —— 与「没设置」不是一回事。"""
        monkeypatch.setattr(user_env, "_run", lambda argv: (1, "", "launchctl: not found"))

        with pytest.raises(user_env.UserEnvError):
            user_env.read_state("HF_ENDPOINT")

    def test_plist_is_valid_xml_and_escapes_user_values(self, macos):
        """手写 XML 的代价：值里有 & < 时必须转义，否则 plist 直接解析不了。"""
        launchctl, agents_dir = macos
        launchctl.env["WEIRD"] = "a&b<c>d"

        user_env.set_value("WEIRD", "a&b<c>d")

        import plistlib

        parsed = plistlib.loads(_plist(agents_dir).read_bytes())
        assert parsed["ProgramArguments"] == ["/bin/launchctl", "setenv", "WEIRD", "a&b<c>d"]


# --------------------------------------------------------------------------
# Windows（在 macOS 上跑真实的 Windows 代码路径）
# --------------------------------------------------------------------------


class TestWindows:
    def test_set_writes_registry_and_broadcasts(self, windows):
        registry, ctypes = windows

        user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")

        state = user_env.read_state("HF_ENDPOINT")
        assert state.value == "https://hf-api.gitee.com"
        # 注册表本身就是持久层，永远为真
        assert state.persistent is True
        assert registry.writes == [("HF_ENDPOINT", "https://hf-api.gitee.com")]

        # 广播 WM_SETTINGCHANGE，lParam 必须是宽字符串 "Environment"
        assert len(ctypes.calls) == 1
        call = ctypes.calls[0]
        assert call["msg"] == 0x001A
        assert call["hwnd"] == 0xFFFF
        assert call["lparam"] == "Environment"
        assert call["flags"] == 0x0002  # SMTO_ABORTIFHUNG
        assert call["timeout"] == 5000

    def test_unset_reads_as_none(self, windows):
        assert user_env.read_state("HF_ENDPOINT").value is None

    def test_clear_is_idempotent(self, windows):
        registry, _ctypes = windows
        user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")

        user_env.clear_value("HF_ENDPOINT")
        assert user_env.read_state("HF_ENDPOINT").value is None
        assert user_env.read_state("HF_ENDPOINT").value is None  # 再来一次不报错
        assert registry.writes == [("HF_ENDPOINT", "https://hf-api.gitee.com")]

    def test_overwrite_replaces_the_value(self, windows):
        user_env.set_value("HF_ENDPOINT", "https://old.example.com")
        user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")

        assert user_env.read_state("HF_ENDPOINT").value == "https://hf-api.gitee.com"

    def test_broadcast_failure_does_not_fail_the_write(self, windows, monkeypatch):
        """广播只是尽力而为的刷新：值已经落进注册表，而重启路径会显式把环境
        变量传给新进程。为它让整个「设置镜像」报失败是误报。"""
        registry, _ctypes = windows
        monkeypatch.setattr(user_env, "_ctypes", FakeCtypes(raise_on_send=True))

        user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")

        assert user_env.read_state("HF_ENDPOINT").value == "https://hf-api.gitee.com"

    def test_registry_write_failure_raises(self, windows, monkeypatch):
        """与广播相反：写不进去必须报错，否则用户以为设好了。"""
        registry, _ctypes = windows

        def boom(*args, **kwargs):
            raise OSError("拒绝访问")

        monkeypatch.setattr(registry, "CreateKeyEx", boom)

        with pytest.raises(user_env.UserEnvError) as excinfo:
            user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")

        assert "注册表" in excinfo.value.user_message

    def test_read_failure_is_not_confused_with_unset(self, windows, monkeypatch):
        """`FileNotFoundError` 才是「没设置」，别的 OSError 是读失败。"""
        registry, _ctypes = windows

        def boom(*args, **kwargs):
            raise PermissionError("拒绝访问")

        monkeypatch.setattr(registry, "OpenKey", boom)

        with pytest.raises(user_env.UserEnvError):
            user_env.read_state("HF_ENDPOINT")


# --------------------------------------------------------------------------
# 平台支持与入参校验
# --------------------------------------------------------------------------


class TestUnsupportedAndValidation:
    def test_linux_is_unsupported(self, linux):
        assert user_env.supported() is False
        with pytest.raises(user_env.UserEnvError) as excinfo:
            user_env.set_value("HF_ENDPOINT", "https://hf-api.gitee.com")
        assert "手动设置" in excinfo.value.user_message

        with pytest.raises(user_env.UserEnvError):
            user_env.read_state("HF_ENDPOINT")
        with pytest.raises(user_env.UserEnvError):
            user_env.clear_value("HF_ENDPOINT")

    @pytest.mark.parametrize("bad", ["", "   ", "a\nb", "a\rb", "a\x00b"])
    def test_bad_values_are_rejected(self, macos, bad):
        """换行能往 plist 的 XML 里拱出新节点，必须在入口拦住。"""
        launchctl, agents_dir = macos

        with pytest.raises(user_env.UserEnvError):
            user_env.set_value("HF_ENDPOINT", bad)

        assert not _plist(agents_dir).exists()
        assert launchctl.commands == [], "值都没通过校验，不该去碰 launchctl"
