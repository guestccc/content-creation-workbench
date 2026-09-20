"""重启 Voicebox 桌面端 —— **唯一**一处会起 / 杀 Voicebox 进程的代码。

与 `voicebox_client` 的边界
===========================
那个模块的 docstring 写着「我们起子进程、不解析 stdout、不管它的生命周期，只发 HTTP」，
这条边界仍然成立：**本模块不管 HTTP**，只管道进程，而且两个模块**互不导入**
（就绪与否由前端轮询自检接口判断，不由这里等）。

为什么必须重启
==============
模型下载源（`HF_ENDPOINT`）是 Voicebox 进程启动那一刻读进内存的，改完环境变量
不重启就不生效。这不是「顺手加个便利按钮」，而是那个功能能成立的必要条件 ——
没有它，「设置镜像」点了等于没点，用户会以为功能坏了。

为什么不能只退出启动器
======================
本机实测（macOS）：`osascript -e 'quit app "Voicebox"'` 只退掉 `Voicebox.app` 的
主进程，`voicebox-server` 会变成 PPID=1 的孤儿继续占着 17493 端口。这时重新
`open -a Voicebox`，新启动器会**复用那个还在跑的旧服务** —— 新设的环境变量对它
无效，用户看到的就是「重启了但没生效」。所以退出之后必须显式清掉残留的 server。

为什么不等它就绪
================
实测冷启动到 `/health` 可用约 30 秒，而前端 fetch 超时是 15 秒 —— 在这里同步等
必然撞超时。就绪交给前端轮询自检接口（与仓库「任务异步跑、进度靠轮询」一致）。
本模块的调用总耗时预算 < 12 秒（见 tests/test_voicebox_restart.py 里那条断言）。
"""

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from app.core.logging import get_logger
from app.core.platform import platform_key
from app.services import voicebox_mirror
from app.services.media_tools import run_probe

logger = get_logger(__name__)

#: 单条命令的超时（秒）。`osascript quit` 会等应用真的退出，慢一点；其余是毫秒级。
_RUN_TIMEOUT_SECONDS = 8.0

#: SIGTERM 之后等多久才上 SIGKILL。
_KILL_GRACE_SECONDS = 3.0

#: 轮询「进程还在不在」的间隔。
_POLL_INTERVAL_SECONDS = 0.1

#: macOS 上 Voicebox 的 bundle id（`Contents/Info.plist` 里读到的）。
#: 用它做 Spotlight 查询，能覆盖「没装在两个标准位置」的情况。
_MACOS_BUNDLE_ID = "sh.voicebox.app"

#: Windows 上的可执行文件名（Tauri 的 NSIS / MSI 包都用 productName 命名）。
_WINDOWS_EXE_NAME = "Voicebox.exe"

#: 与桌面端一起分发的服务进程。它可能被「保持后台服务运行」这个偏好留在后台
#: 独立存活，`taskkill /T` 的进程树杀法够不到它，得按映像名补一刀。
_WINDOWS_SERVER_EXE_NAME = "voicebox-server.exe"

#: Windows 上把子进程彻底摘出去的标志位。DETACHED_PROCESS：不继承我们的控制台，
#: 关掉后端不会连带带走 Voicebox；CREATE_NEW_PROCESS_GROUP：不吃控制台的 Ctrl+C。
#: POSIX 上为 0（macOS 走 `open`，用不到）。
_DETACHED_FLAGS = (
    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    if os.name == "nt"
    else 0
)


class RestartError(Exception):
    """重启失败的统一形态：`user_message` 给人看，`detail` 进日志。

    形状照 `voicebox_client.VoiceboxError`：本模块不抛 `AppException` 子类，
    HTTP 状态码的翻译留在 API 层。
    """

    def __init__(self, user_message: str, *, detail: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


class VoiceboxNotFoundError(RestartError):
    """没找到 Voicebox 装在哪 —— 找不到就没法可靠地重启它（API 层翻成 404）。"""


@dataclass(frozen=True)
class RestartResult:
    """一次重启的结果。**不代表服务已就绪**（就绪靠前端轮询）。"""

    app_path: str
    """拉起的安装位置；空串表示按名字查找拉起的（macOS 的 `open -a` 降级路径）。"""

    killed_pids: List[int] = field(default_factory=list)
    """清掉的残留服务进程，只进日志与自检详情，不进页面文案。"""

    detail: str = ""
    """给用户看的一句话。"""


# --------------------------------------------------------------------------
# 可替换点（测试隔离的接口）
# --------------------------------------------------------------------------


def _run(argv: Sequence[str]) -> Tuple[int, str, str]:
    """跑一条命令，返回 `(rc, stdout, stderr)`，从不抛异常。

    与 `user_env._run` 一样走 `media_tools.run_probe`（同一个 subprocess 教训），
    只是超时预算不同。
    """
    probed = run_probe(argv, timeout=_RUN_TIMEOUT_SECONDS)
    if probed is None:
        return 1, "", f"命令执行失败或超时：{' '.join(argv)}"
    return probed.returncode, probed.stdout, probed.stderr


def _popen(argv: Sequence[str], *, env: Dict[str, str]) -> None:
    """拉起一个**不跟我们一起死**的进程（测试整体替换它）。

    为什么不能用 `os.startfile` / `open`：它们不接受 `env` 参数，而 Windows 上
    必须显式把环境变量传给子进程（理由见 `_child_env`）。
    """
    subprocess.Popen(  # noqa: S603 - argv 由本模块构造，不含用户输入
        list(argv),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=_DETACHED_FLAGS,
    )


def _list_processes() -> List[Tuple[int, int, str]]:
    """列出本机进程：`[(pid, ppid, command)]`。测试整体替换它，不去真跑 ps。"""
    rc, out, err = _run(["ps", "-axo", "pid=,ppid=,command="])
    if rc != 0:
        logger.warning("读取进程列表失败，将无法清理残留服务进程 | %s", err.strip())
        return []
    processes: List[Tuple[int, int, str]] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            processes.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return processes


# Windows 的 winreg 与 macOS 的进程信号是两套东西，这里独立导入一次。
# （「单一来源」那条约定管的是**判断逻辑**，不是 import 语句；
#   `user_env` 那份 winreg 是给环境变量用的，与这里的卸载登记表反查无关。）
if os.name == "nt":  # pragma: no cover - 只在 Windows 上走这一支
    import winreg as _winreg
else:
    _winreg = None


# --------------------------------------------------------------------------
# 定位安装位置
# --------------------------------------------------------------------------


def _windows_registry_candidates() -> List[Path]:
    """从卸载登记表反查安装位置。

    依据：Tauri 的 NSIS / WiX 安装包都会在
    `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\<...>`
    （perMachine 安装时在 HKLM）下写 DisplayName / InstallLocation / DisplayIcon。
    用户装在非默认目录时，这是唯一可靠的线索。

    任何异常都吞掉返回空列表：反查只是兜底，它失败不该让整个「找 Voicebox」失败。
    """
    if _winreg is None:  # pragma: no cover - 非 Windows 上恒为真
        return []
    found: List[Path] = []
    uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for root in (_winreg.HKEY_CURRENT_USER, _winreg.HKEY_LOCAL_MACHINE):
        try:
            with _winreg.OpenKey(root, uninstall) as key:
                sub_count = _winreg.QueryInfoKey(key)[0]
                sub_names = [_winreg.EnumKey(key, i) for i in range(sub_count)]
        except OSError:
            continue
        for name in sub_names:
            try:
                with _winreg.OpenKey(root, f"{uninstall}\\{name}") as sub:
                    display = str(_winreg.QueryValueEx(sub, "DisplayName")[0])
                    if "voicebox" not in display.casefold():
                        continue
                    location = _query_optional(sub, "InstallLocation")
                    if location:
                        found.append(Path(location) / _WINDOWS_EXE_NAME)
                    icon = _query_optional(sub, "DisplayIcon")
                    if icon:
                        # DisplayIcon 常见形如 `C:\...\Voicebox.exe,0`，把索引后缀去掉
                        candidate = Path(icon.split(",")[0].strip().strip('"'))
                        if candidate.suffix.lower() == ".exe":
                            found.append(candidate)
            except OSError:
                continue
    return found


def _query_optional(key, name: str) -> str:  # pragma: no cover - 仅 Windows
    """读一个可能不存在的注册表值，读不到返回空串。"""
    try:
        return str(_winreg.QueryValueEx(key, name)[0])
    except OSError:
        return ""


def _macos_candidates() -> List[Path]:
    """候选安装路径（按优先级）。两个标准位置就覆盖了绝大多数安装。"""
    return [
        Path("/Applications/Voicebox.app"),
        Path.home() / "Applications" / "Voicebox.app",
    ]


def _windows_candidates() -> List[Path]:
    """候选安装路径（按优先级），末尾接注册表反查出来的。

    依据：Tauri v2 的 **NSIS 默认 `installMode=currentUser`**，装到
    `%LOCALAPPDATA%\\<productName>` 且不需要管理员权限；`perMachine`（MSI 或显式
    配置）才装到 `Program Files`。Voicebox 的 `tauri.conf.json` 里
    `bundle.windows` 是空的、`targets: "all"`，所以 NSIS 与 MSI 两种包都会发，
    两条路径都得猜。安装目录名与可执行文件名都等于 `productName`。
    """
    local = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("PROGRAMFILES", "")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", "")
    candidates = [
        Path(local) / "Voicebox" / _WINDOWS_EXE_NAME,
        Path(local) / "Programs" / "Voicebox" / _WINDOWS_EXE_NAME,
        Path(program_files) / "Voicebox" / _WINDOWS_EXE_NAME,
        Path(program_files_x86) / "Voicebox" / _WINDOWS_EXE_NAME,
    ]
    # 环境变量缺失时 Path("") 会变成相对路径 "."，过滤掉，别让它去匹配当前目录
    candidates = [p for p in candidates if p.is_absolute()]
    return [*candidates, *_windows_registry_candidates()]


def _macos_spotlight_candidates() -> List[Path]:
    """用 Spotlight 按 bundle id 查安装位置。

    覆盖「装在两个标准位置之外」（自定义目录、外置卷）。用 `mdfind` 而不是
    AppleScript 的 `path to application`：后者在应用没注册时**会弹一个选择文件的
    对话框**把请求线程挂住，`mdfind` 只查询、永远不会弹窗。
    """
    rc, out, _err = _run(
        ["mdfind", f"kMDItemCFBundleIdentifier == '{_MACOS_BUNDLE_ID}'"]
    )
    if rc != 0:
        return []
    return [Path(line.strip()) for line in out.splitlines() if line.strip()]


def _candidates() -> List[Path]:
    """当前平台的全部候选路径（已去重、保持优先级顺序）。"""
    if platform_key() == "macos":
        raw = [*_macos_candidates(), *_macos_spotlight_candidates()]
    elif platform_key() == "windows":
        raw = _windows_candidates()
    else:
        raw = []
    unique: List[Path] = []
    for path in raw:
        if path not in unique:
            unique.append(path)
    return unique


def find_app() -> Optional[Path]:
    """找到 Voicebox 的安装位置；找不到返回 None。

    **不做缓存**：用户可能刚装上 Voicebox，缓存住「没找到」会让页面一直说
    「没找到安装位置」。代价是每次探测多一次 `mdfind`（本机实测 75ms），
    而探测本身有 30 秒 TTL 缓存兜着。
    """
    for path in _candidates():
        if path.exists():
            return path
    return None


def searched_paths() -> List[str]:
    """「都找过哪些地方」——报错文案里要**列全**，不能只说最后一个。

    这是 subtitle_env 那边踩过的教训：只报最后一条候选，用户会以为工具没找
    他装的那个位置，然后自己重装一遍。
    """
    descriptions = [str(path) for path in _candidates()]
    if platform_key() == "macos":
        descriptions.append(f"Spotlight 查询（bundle id {_MACOS_BUNDLE_ID}）")
    else:
        descriptions.append("注册表卸载登记表（HKCU / HKLM 的 Uninstall）")
    return descriptions


# --------------------------------------------------------------------------
# 退出（含清理残留的服务进程）
# --------------------------------------------------------------------------


def _is_alive(pid: int) -> bool:
    """进程还在不在。`os.kill(pid, 0)` 不发信号，只做存在性与权限检查。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不是我们的 —— 照样算「还在」
    return True


def _terminate(pid: int, grace: float = _KILL_GRACE_SECONDS) -> None:
    """先 SIGTERM，宽限期内没退再 SIGKILL。**任何异常都吞掉。**

    不复用 `media_tools.terminate_process_group`：那个函数对 pid **所在进程组**
    发 `killpg`，面向的是我们自己用 `start_new_session=True` 建出来的进程组；
    这里要清的孤儿不是我们创建的，killpg 会连带打到不相干的进程上。
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _looks_like_server(command: str) -> bool:
    """这条命令行像是 Voicebox 的服务进程吗（含它拉起的 multiprocessing 助手）。"""
    return f"{os.sep}voicebox-server" in command or command.startswith("voicebox-server")


def _leftover_server_pids(app: Optional[Path]) -> List[int]:
    """找出退出启动器之后、必须一并清掉的 `voicebox-server` 进程。

    两条判据，命中任意一条即算：

    1. **可执行文件在同一个 `.app` 内**（`{app}/Contents/MacOS/voicebox-server`）。
       最精确，且能覆盖已孤儿化的情况。知道 app 路径时**强制**用路径前缀，
       免得误杀用户自己从源码跑起来的 server；
    2. **命令行里有 `--parent-pid <启动器 PID>`**。兜底，覆盖「运行中的实例不是
       我们找到的那份安装」（比如 `/Applications` 与 `~/Applications` 各有一份）。
    """
    processes = _list_processes()
    launcher_pids = {
        pid
        for pid, _ppid, command in processes
        if app is not None
        and str(app) in command
        and f"{os.sep}voicebox-server" not in command
    }
    # 已知两份安装都在时，用两个 .app 前缀都认一遍，别漏掉在跑的那份
    prefixes = [f"{path}{os.sep}Contents{os.sep}MacOS{os.sep}voicebox-server" for path in _candidates()]

    leftover: List[int] = []
    for pid, _ppid, command in processes:
        if not _looks_like_server(command):
            continue
        if any(prefix in command for prefix in prefixes):
            leftover.append(pid)
            continue
        if any(f"--parent-pid {launcher}" in command for launcher in launcher_pids):
            leftover.append(pid)
            continue
        logger.debug("跳过不像 Voicebox 安装的 server 进程 | pid=%s | %s", pid, command[:120])
    return leftover


def _quit_macos(app: Path) -> None:
    """让 Voicebox 正常退出。

    `osascript` 的 `quit` 会等应用真的退出再返回；应用没在跑时返回非 0，
    这是正常状态，忽略。
    """
    _run(["osascript", "-e", 'quit app "Voicebox"'])


def _quit_windows() -> None:
    """退出 Voicebox：先温和，再强制。

    先不带 `/F` 让 Tauri 走正常退出（它会自己收掉 sidecar）；脚本化的退出不一定
    成功（桌面端崩了、或用户开着「保持后台服务运行」），所以补一轮 `/F` 按映像名
    强杀。`/T` 只覆盖进程树，对已孤儿化的 server 无效 —— 那一刀必须按映像名补。
    """
    _run(["taskkill", "/IM", _WINDOWS_EXE_NAME, "/T"])
    _run(["taskkill", "/F", "/IM", _WINDOWS_EXE_NAME])
    _run(["taskkill", "/F", "/IM", _WINDOWS_SERVER_EXE_NAME])


# --------------------------------------------------------------------------
# 拉起
# --------------------------------------------------------------------------


def _child_env() -> Dict[str, str]:
    """给新进程的环境变量。

    **Windows 上这是本模块最关键的一处**：镜像值刚写进注册表，但**本进程的环境块
    在启动时就固定了**，子进程继承的是我们的旧环境块 —— 不显式带上，重启完
    Voicebox 还是看不到镜像，用户看到的就是「设了、也重启了，还是没生效」。

    没设镜像时**必须把可能残留的 `HF_ENDPOINT` 删掉**：否则用户点了「清除镜像」
    再重启，新进程仍会从我们的环境块里继承到旧值，「清除」等于没清。
    """
    env = dict(os.environ)
    value = voicebox_mirror.current_value()
    if value:
        env[voicebox_mirror.HF_MIRROR_KEY] = value
    else:
        env.pop(voicebox_mirror.HF_MIRROR_KEY, None)
    return env


def _launch_macos(app: Path) -> None:
    """用 `open` 拉起。它没有传 env 的参数，靠的是镜像值已经通过
    `launchctl setenv` 写进**图形会话**的环境、LaunchServices 拉起时读它 ——
    这条路径在本机端到端验证过（进程环境里能看到 HF_ENDPOINT）。"""
    _run(["open", "-a", str(app)])


def _launch_windows(app: Path) -> None:
    """显式带 env 直接起 exe（Windows 上没有 macOS 那样的会话级环境注入兜底）。"""
    _popen([str(app)], env=_child_env())


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


def restart() -> RestartResult:
    """重启 Voicebox：退出 → 清残留服务进程 → 重新拉起。

    **不等就绪**（就绪约 30 秒，前端 fetch 15 秒就超时了），返回后由前端轮询
    自检接口直到 `reachable`。

    Raises:
        VoiceboxNotFoundError: 没找到安装位置。
        RestartError: 平台不支持。
    """
    platform = platform_key()
    app = find_app()
    if app is None:
        raise VoiceboxNotFoundError(
            "没有找到 Voicebox 的安装位置，无法自动重启。已查找："
            + "、".join(searched_paths())
            + "。请手动打开 Voicebox 后回到本页点「重新检测」。"
        )

    if platform == "macos":
        _quit_macos(app)
        leftover = _leftover_server_pids(app)
        for pid in leftover:
            _terminate(pid)
        _launch_macos(app)
        return RestartResult(
            app_path=str(app),
            killed_pids=leftover,
            detail="Voicebox 已重新启动，正在等它把服务拉起来。",
        )

    if platform == "windows":
        _quit_windows()
        _launch_windows(app)
        return RestartResult(
            app_path=str(app),
            detail="Voicebox 已重新启动，正在等它把服务拉起来。",
        )

    raise RestartError("当前系统不支持自动重启 Voicebox，请手动退出后重新打开它。")
