"""用户的**用户级**环境变量：读、写、清（macOS 与 Windows 两套实现）。

为什么需要这个模块
==================
Voicebox 从 huggingface.co 拉模型（1.7B 约 3.5GB），国内直连会卡死。解法是给它
设 `HF_ENDPOINT` 指向镜像，但麻烦在于**设在哪一层才有用**：

- 写进 `.env` 没用 —— 那是我们后端自己的配置，Voicebox 是另一个进程；
- 写进 `~/.zshrc` 也没用 —— macOS 上 GUI 应用由 LaunchServices 拉起，
  **根本不继承 shell 环境**。本机实测：没做下面的注入时，`voicebox-server`
  进程的环境里只有 `PATH=/usr/bin:/bin:/usr/sbin:/sbin`；
- 只有**用户级环境变量**这一层，才是桌面应用与之后就绪的进程都能拿到的。

两个平台的落点不同，差异全部收在本模块里：

| | macOS | Windows |
| --- | --- | --- |
| 落点 | `~/Library/LaunchAgents/<label>.plist`（内容就是跑 `launchctl setenv`）+ 当前图形会话 | 注册表 `HKCU\\Environment` |
| 生效方式 | `launchctl bootstrap` 装载，`RunAtLoad` 立刻执行一次；`kickstart -k` 可重跑 | 写完后广播 `WM_SETTINGCHANGE`，Explorer 重读注册表刷新自己的环境块 |
| 对**已启动**的进程 | 无效 | 无效 |

最后一行是两边共同、也最容易被误解的一点：**环境变量只对之后新启动的进程生效**。
所以页面上设完镜像必须跟着一个「重启 Voicebox」的动作，否则用户会以为「设了没用」。
（我们这个后端进程自己也在内 —— 它的环境块在启动时就固定了。）

设计约束
========
1. **只在能可靠做到的系统上给结论**：Linux 上两个落点都不存在，`supported()`
   返回 False，调用方据此藏掉按钮、改说「请手动 export」，而不是假装成功；
2. **失败一律抛 `UserEnvError`，不静默**：写不进去却报成功，用户会在半小时后
   撞上「模型还是下不动」，那时更难查；
3. **测试绝不碰真的 launchd 与注册表**：`_run` / `_LAUNCH_AGENTS_DIR` / `_winreg`
   / `_ctypes` 四个模块属性就是全部的隔离点，测试整体替换它们（见
   tests/test_user_env.py）。真实实现里 `subprocess` 只出现在 `_run` 一处。

函数名为什么不叫 `set` / `clear`：那会遮蔽内建 `set`，本模块里任何一处 `set(...)`
都可能被误读。`set_value` / `clear_value` 多打几个字符，换掉一个掉进去很难看出来的坑。
"""

import os
import time
import xml.sax.saxutils
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from app.core import env_file
from app.core.logging import get_logger
from app.core.platform import platform_key
from app.services.media_tools import run_probe

logger = get_logger(__name__)

#: 子进程超时（秒）。launchctl 是本地 IPC，正常是毫秒级；给足余量但不无限等 ——
#: 这个函数在 HTTP 请求线程里被调用，卡住就是页面卡住。
_RUN_TIMEOUT_SECONDS = 8.0

#: 我们自己的 LaunchAgent 标签。用工具自己的域名，不蹭 Voicebox 的 ——
#: 卸载 / 排查时一眼能看出这个 job 是谁装的。
LAUNCH_AGENT_LABEL = "com.content-workbench.user-env"

#: macOS 的图形会话域（`gui/<uid>`）。launchctl setenv 只影响这个域的会话，
#: 而页面要注入的正是桌面应用。
_GUI_DOMAIN_PREFIX = "gui"


# --------------------------------------------------------------------------
# 可替换点（测试隔离的全部接口）
# --------------------------------------------------------------------------

#: LaunchAgents 目录。测试 monkeypatch 这个**模块属性**（与 voicebox_settings._ENV_PATH
#: 同一手法），所以函数里必须在调用时读它，不能固化进函数默认值。
_LAUNCH_AGENTS_DIR: Path = Path.home() / "Library" / "LaunchAgents"

#: winreg / ctypes 只在 Windows 上存在。留成模块属性是为了让测试能在 macOS 上
#: 塞假模块进来，把真实的 Windows 代码路径跑一遍（见 tests/test_user_env.py）。
if os.name == "nt":  # pragma: no cover - 只在 Windows 上走这一支
    import ctypes as _ctypes
    import winreg as _winreg
else:
    _ctypes = None
    _winreg = None


def _run(argv: List[str]) -> Tuple[int, str, str]:
    """跑一条命令，返回 `(returncode, stdout, stderr)`，**从不抛异常**。

    真正的执行走 `media_tools.run_probe`（按字节捕获、自己解码、失败返回 None）——
    那套「不给 subprocess 传 text=True」的教训只有一份。这里只把它的
    `Optional[ProbedOutput]` 契约翻译成「起不来也算 rc=1」：调用方只看 rc，
    「命令根本没有」与「命令跑了但失败了」对它们是一回事，都该报错。
    """
    probed = run_probe(argv, timeout=_RUN_TIMEOUT_SECONDS)
    if probed is None:
        return 1, "", f"命令执行失败或超时（{_RUN_TIMEOUT_SECONDS:.0f} 秒）：{' '.join(argv)}"
    return probed.returncode, probed.stdout, probed.stderr


# --------------------------------------------------------------------------
# 对外类型与入口
# --------------------------------------------------------------------------


class UserEnvError(Exception):
    """设置用户级环境变量失败的统一形态：`user_message` 给人看，`detail` 进日志。

    形状照 services/voicebox_client.py 的 `VoiceboxError`：本模块不抛
    `AppException` 子类，HTTP 状态码的翻译留在 API 层 —— 服务层不该知道 HTTP。
    """

    def __init__(self, user_message: str, *, detail: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


@dataclass(frozen=True)
class UserEnvValue:
    """一个用户级环境变量的现状。"""

    value: Optional[str]
    """当前值；None 表示未设置（与空串区分 —— 空串是一次「显式设成空」）。"""

    persistent: bool
    """注销 / 重启后是否还在（macOS 看 plist 在不在；Windows 的注册表本身就是持久层）。"""


def supported() -> bool:
    """当前系统能不能由本模块设置用户级环境变量。

    Linux 没有对应的落点：桌面应用怎么拿到环境变量取决于它自己怎么被拉起
    （systemd --user、桌面环境的 .desktop 文件各有各的写法），我们没有可靠做法，
    就如实说不支持，让调用方改出「请手动 export」的文案。
    """
    return platform_key() in ("macos", "windows")


def _assert_supported() -> None:
    if not supported():
        raise UserEnvError(
            "当前系统暂不支持由本工具设置环境变量：请在启动 Voicebox 前手动设置它。"
        )


def _assert_value(value: str, key: str) -> None:
    """拦掉写进去会出事、或根本不该出现的值。

    换行最要紧：它能往 plist 的 XML 里拱出新节点。首尾空白与空串也一并拒绝 ——
    环境变量的值本来就允许有空格，但「值只有空格」一定是调用方拼错了。
    """
    if not value.strip():
        raise UserEnvError(f"{key} 的值不能为空")
    bad = [ch for ch in value if ch in "\r\n\x00" or ord(ch) < 0x20]
    if bad:
        raise UserEnvError(f"{key} 的值里不能有换行或控制字符")


def read_state(key: str) -> UserEnvValue:
    """读一个用户级环境变量的现状。

    Raises:
        UserEnvError: 平台不支持，或读取失败（读不出来 ≠ 没设置，不能混为一谈）。
    """
    _assert_supported()
    platform = platform_key()
    if platform == "macos":
        # persistent 看 plist 在不在：值可能来自用户自己的 launchctl setenv，
        # 那种「设了但重启就没了」的状态必须如实报出来，页面才能提示。
        return UserEnvValue(value=_read_macos(key), persistent=_plist_path().exists())
    return UserEnvValue(value=_read_windows(key), persistent=True)


def set_value(key: str, value: str) -> None:
    """设置用户级环境变量（幂等：重复设同一个值不报错）。

    Raises:
        UserEnvError: 平台不支持 / 值非法 / 写入后验证不通过。
    """
    _assert_supported()
    _assert_value(value, key)
    platform = platform_key()
    if platform == "macos":
        _set_macos(key, value)
    else:
        _set_windows(key, value)
    logger.info("用户级环境变量已设置 | %s | 落点=%s", key, "plist" if platform == "macos" else "注册表")


def clear_value(key: str) -> None:
    """清除用户级环境变量（幂等：本来就没有也算成功）。

    Raises:
        UserEnvError: 平台不支持 / 清除后仍能读到旧值。
    """
    _assert_supported()
    if platform_key() == "macos":
        _clear_macos(key)
    else:
        _clear_windows(key)
    logger.info("用户级环境变量已清除 | %s", key)


# --------------------------------------------------------------------------
# macOS：LaunchAgent（launchctl setenv 的持久化）
# --------------------------------------------------------------------------


def _plist_path() -> Path:
    """我们的 LaunchAgent plist 路径。**调用时**读 `_LAUNCH_AGENTS_DIR`。"""
    return _LAUNCH_AGENTS_DIR / f"{LAUNCH_AGENT_LABEL}.plist"


def _gui_domain() -> str:
    """图形会话域，如 `gui/501`。"""
    return f"{_GUI_DOMAIN_PREFIX}/{os.getuid()}"


def _session_ok() -> bool:
    """后端进程能不能看到用户的图形登录会话。

    看不到（后端由 launchd / SSH / sudo 启动）就没法给桌面应用注入变量，
    此时应当当场说清楚并给出可照抄的手工命令 —— 不然后面每一步都会以
    「bootstrap 失败」这种看不出原因的方式失败。
    """
    rc, _out, _err = _run(["launchctl", "print", _gui_domain()])
    return rc == 0


def _plist_xml(key: str, value: str) -> str:
    """生成 plist 内容。

    手写模板而不是 `plistlib.dumps`：这个文件躺在 `~/Library/LaunchAgents/` 里，
    用户排查问题时会直接打开看，一段说明「为什么要这么写」的注释比什么都值钱，
    而 plistlib 生成不了注释。代价是要自己转义 —— 用 xml.sax.saxutils 做，
    并且写盘前用 plistlib **回读校验一次**，两头都占上。
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!--
      让 GUI 应用也能拿到 {key}。
      GUI 应用由 LaunchServices 拉起，不继承 shell 环境，所以写在 ~/.zshrc 里没用；
      必须用 launchctl setenv 注入用户会话的环境。RunAtLoad 让它在每次登录时执行。
      这个文件由「内容制作工具」生成；在页面上点「清除镜像」会删掉它。
    -->
    <key>Label</key>
    <string>{LAUNCH_AGENT_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/launchctl</string>
        <string>setenv</string>
        <string>{xml.sax.saxutils.escape(key)}</string>
        <string>{xml.sax.saxutils.escape(value)}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""


def _read_macos(key: str) -> Optional[str]:
    """`launchctl getenv` 读当前图形会话里的值；未设置时返回 None。

    注意这是**会话**环境，不是 plist 的内容 —— 两者不一致是可能的
    （有人手改过 plist 但没重跑 job），所以调用方要拿 persistent 一起看。
    """
    rc, out, err = _run(["launchctl", "getenv", key])
    if rc != 0:
        raise UserEnvError(
            "读取系统环境变量失败（launchctl getenv 不可用）", detail=err.strip()
        )
    value = out.strip()
    return value or None


def _set_macos(key: str, value: str) -> None:
    """落 plist 并让它立刻执行一次，最后**读回来验证**。"""
    if not _session_ok():
        raise UserEnvError(
            "当前后端进程不在图形登录会话里（可能由 launchd / SSH / sudo 启动），"
            f"无法给桌面应用注入环境变量。请手动在终端执行：launchctl setenv {key} {value}",
        )

    path = _plist_path()
    xml_text = _plist_xml(key, value).encode("utf-8")
    _assert_plist_parses(xml_text, path)
    env_file.atomic_write(
        path, xml_text, log_label=f"{key} LaunchAgent", tmp_prefix=".user-env.", tmp_suffix=".tmp"
    )

    # 幂等：先 bootout 再 bootstrap。**bootout 对「本来就没装载」会返回非 0 ——
    # 这是预期，不报错**（第一次设置时必然命中这一支）。
    _run(["launchctl", "bootout", f"{_gui_domain()}/{LAUNCH_AGENT_LABEL}"])
    rc, _out, err = _run(["launchctl", "bootstrap", _gui_domain(), str(path)])
    if rc != 0:
        # 已装载时 bootstrap 也会失败（本机实测）。kickstart 重跑一次已装载的 job
        # 兜底 —— 但如果 plist 内容变了，kickstart 跑的还是**旧**的 ProgramArguments，
        # 所以这只是尽力而为，真正的判据是下面的读回验证。
        rc2, _out2, err2 = _run(
            ["launchctl", "kickstart", "-k", f"{_gui_domain()}/{LAUNCH_AGENT_LABEL}"]
        )
        if rc2 != 0:
            raise UserEnvError(
                "设置系统环境变量失败：LaunchAgent 装载未成功",
                detail=f"bootstrap: {err.strip()} / kickstart: {err2.strip()}",
            )

    if not _wait_until(lambda: _read_macos(key) == value):
        raise UserEnvError(
            f"环境变量写入后未生效（{key} 读回来的不是新值）",
            detail=f"plist: {path}",
        )


#: 读回验证的重试参数。
#:
#: `launchctl bootstrap` 是**异步**的：它把 job 交给 launchd，launchd 之后再找机会
#: 执行 ProgramArguments 里那条 `launchctl setenv`。所以 bootstrap 返回后紧接着读
#: `getenv`，可能读到的还是旧值 —— 本机实测：先「清除镜像」再「设置镜像」必然撞上
#: （清完会话里是空的，读到空就被当成「没生效」）。
#:
#: 这个坑单测发现不了：假 launchctl 的 bootstrap 是**同步**跑完 plist 的，比现实更
#: 快一步。所以这里必须重试，而不是「读一次不对就报错」—— 要区分的是两件事：
#: 「还没来得及生效」（等一下就好）和「设了也没用」（必须报错）。
#: 20 × 50ms = 1 秒上限，用户无感；命令本身超时另算。
_VERIFY_ATTEMPTS = 20
_VERIFY_INTERVAL_SECONDS = 0.05


def _wait_until(predicate) -> bool:
    """轮询到 `predicate()` 为真；用尽次数仍为假则返回 False。

    `predicate` 抛异常时**直接往外抛**：那是 launchctl 本身不可用（比如 rc≠0），
    不是「还没生效」，重试到天荒地老也不会变好。
    """
    for attempt in range(_VERIFY_ATTEMPTS):
        if predicate():
            return True
        if attempt + 1 < _VERIFY_ATTEMPTS:
            time.sleep(_VERIFY_INTERVAL_SECONDS)
    return False


def _clear_macos(key: str) -> None:
    """卸载 job、清会话里的值、删 plist —— 三步都容忍「本来就没有」。"""
    _run(["launchctl", "bootout", f"{_gui_domain()}/{LAUNCH_AGENT_LABEL}"])
    _run(["launchctl", "unsetenv", key])
    try:
        _plist_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise UserEnvError("删除 LaunchAgent 文件失败", detail=str(exc)) from exc

    if not _wait_until(lambda: _read_macos(key) is None):
        # 值可能是别的 LaunchAgent / 用户手动 setenv 留下的，我们清不掉也不该假装清掉了
        raise UserEnvError(
            f"清除后仍能读到 {key} 的值：它可能是用户手动设置的（或需要注销重新登录才生效）"
        )


def _assert_plist_parses(data: bytes, path: Path) -> None:
    """写盘前回读校验：手写的 XML 有语法错时，launchd 只会给一句含糊的
    「Bootstrap failed: 5: Input/output error」，不如在这里就拦住。"""
    import plistlib

    try:
        plistlib.loads(data)
    except Exception as exc:  # noqa: BLE001 - plistlib 的异常类型不统一，这里只要「解析不了」
        raise UserEnvError("生成 LaunchAgent 配置失败（plist 格式不合法）", detail=f"{path}: {exc}") from exc


# --------------------------------------------------------------------------
# Windows：注册表 HKCU\Environment + WM_SETTINGCHANGE 广播
# --------------------------------------------------------------------------

#: 用户级环境变量在注册表里的位置。`setx` 写的正是这里 —— 我们绕过 setx 走
#: 注册表 API，因为 setx 会在 1024 字符处截断、会对含特殊字符的值二次转义、
#: 还会弹一个控制台窗口。
_ENV_KEY_PATH = "Environment"

#: WM_SETTINGCHANGE 的窗口消息号与「广播给所有顶层窗口」的伪句柄（微软文档）。
_WM_SETTINGCHANGE = 0x001A
_HWND_BROADCAST = 0xFFFF
#: 目标窗口 5 秒内没处理完消息就放弃，别把我们的请求线程搭进去。
_SMTO_ABORTIFHUNG = 0x0002


def _read_windows(key: str) -> Optional[str]:
    """读 `HKCU\\Environment` 下的一个值；不存在返回 None。"""
    try:
        with _winreg.OpenKey(
            _winreg.HKEY_CURRENT_USER, _ENV_KEY_PATH, 0, _winreg.KEY_READ
        ) as handle:
            value, _kind = _winreg.QueryValueEx(handle, key)
    except FileNotFoundError:
        # 键或值不存在 = 没设置。这不是错误，与「读失败」必须分开。
        return None
    except OSError as exc:
        raise UserEnvError(
            "读取用户环境变量失败（注册表 HKCU\\Environment）", detail=str(exc)
        ) from exc
    return str(value)


def _set_windows(key: str, value: str) -> None:
    """写注册表并广播刷新。"""
    try:
        with _winreg.CreateKeyEx(
            _winreg.HKEY_CURRENT_USER, _ENV_KEY_PATH, 0, _winreg.KEY_SET_VALUE
        ) as handle:
            _winreg.SetValueEx(handle, key, 0, _winreg.REG_SZ, value)
    except OSError as exc:
        raise UserEnvError(
            "写入用户环境变量失败（注册表 HKCU\\Environment）", detail=str(exc)
        ) from exc
    _broadcast_windows()


def _clear_windows(key: str) -> None:
    """删注册表里的值（幂等）并广播刷新。"""
    try:
        with _winreg.OpenKey(
            _winreg.HKEY_CURRENT_USER, _ENV_KEY_PATH, 0, _winreg.KEY_SET_VALUE
        ) as handle:
            _winreg.DeleteValue(handle, key)
    except FileNotFoundError:
        # 本来就没有 —— 幂等，不算失败
        pass
    except OSError as exc:
        raise UserEnvError(
            "删除用户环境变量失败（注册表 HKCU\\Environment）", detail=str(exc)
        ) from exc
    _broadcast_windows()


def _broadcast_windows() -> None:
    """广播 `WM_SETTINGCHANGE`，让 Explorer 重读注册表刷新自己的环境块。

    依据（微软 Environment Variables / WM_SETTINGCHANGE 文档）：关心环境变量变化的
    程序需要监听 WM_SETTINGCHANGE 并检查 lParam 是不是字符串 "Environment"；
    Explorer 收到后会重读注册表，**之后**从开始菜单 / 桌面 / 资源管理器启动的
    进程才继承新值。必须用 `W`（宽字符）版本，否则非 ASCII 值会经 ANSI 代码页
    损坏；`SMTO_ABORTIFHUNG` + 超时防止 Explorer 卡死时把我们挂住。

    **广播失败不抛异常，只记日志**：值已经落进注册表了，而页面驱动的重启路径
    会**显式**把环境变量传给新进程（见 voicebox_restart._child_env），不依赖这次
    广播。为一个尽力而为的刷新动作让整个「设置镜像」报失败，是误报。
    """
    if _ctypes is None:
        return
    try:
        result = _ctypes.c_ulong()
        _ctypes.windll.user32.SendMessageTimeoutW(
            _HWND_BROADCAST,
            _WM_SETTINGCHANGE,
            0,
            _ctypes.c_wchar_p("Environment"),
            _SMTO_ABORTIFHUNG,
            5000,
            _ctypes.byref(result),
        )
    except Exception as exc:  # noqa: BLE001 - ctypes 抛什么都有（属性缺失、权限…），都不该失败整个操作
        logger.warning("广播 WM_SETTINGCHANGE 失败（值已写入注册表，重启 Voicebox 仍会带上）| %s", exc)
