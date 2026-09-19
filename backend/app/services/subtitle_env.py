"""VideoCaptioner 的探测与环境自检。

字幕提取的实际转写由 **VideoCaptioner** 完成（它不在本仓库里，见
`app/core/config.py` 的 SUBTITLE_VC_ROOT 注释）。这个模块回答两个问题：

1. **它装了吗？装在哪？** —— 探测出一条能直接拼 argv 的调用方式；
2. **没装的话让用户怎么装？** —— 按当前系统给出可复制的安装命令。

「怎么调」为什么不走仓库里的 `vct` 包装（`vctl/commands/vc.py`）：
`vct transcribe` 是给终端用的 UI 层（用 rich 打印给人看），而且它的探测
逻辑埋在 `vctl` 包里、只认仓库内的固定路径。我们要的恰恰是「后端自己的
跨平台检测 + 给用户看的安装指引」，所以直接调 VideoCaptioner 自己的 CLI。

探测成本与副作用（实测）：
- `videocaptioner --version` 约 1.2 秒 —— 它会把 dub/process 那几套子命令的
  解析器都建起来，太重；
- `python -c "..."` 只导入包、打印版本与配置文件路径，约 0.1 秒，用它；
- 导入 `videocaptioner.cli.config` 会在用户 HOME 下创建配置目录。这是被调
  程序的行为，发生在我们起的**子进程**里；但正因为有副作用，结果必须缓存，
  不能每个请求都探一次。

安全说明：本模块只**探测**，绝不执行安装 —— 安装命令只是回给前端展示的
文本。后端替网页执行 `pip install` 这类操作，等于把「谁能打开这个页面」
变成了「谁能在这台机器上装东西」。
"""

import json
import os
import platform
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Optional, Set, Tuple

from app.core.config import repo_root, settings, toolbox_root
from app.core.logging import get_logger
from app.core.materials import SOURCE, SUBTITLE, materials_root, subdir
from app.services.media_tools import find_tool, run_probe
from app.services.subtitle_settings import shadowing_warning, vc_root_source

logger = get_logger(__name__)

#: 探测子进程的超时（秒）。首次导入包可能稍慢，但也不该无限等。
PROBE_TIMEOUT_SECONDS = 20

#: 被调包的导入名与分发名。两个不一样：前者用于 import，后者用于查版本。
PACKAGE_MODULE = "videocaptioner"
PACKAGE_DIST = "videocaptioner"

#: 在目标解释器里跑的小脚本：确认包在、打印版本与配置文件路径。
#: 输出走 stdout 的最后一行 JSON —— 前面可能有 import 期间打的警告。
_PROBE_SCRIPT = f"""
import json, sys
import importlib.util as util

if util.find_spec({PACKAGE_MODULE!r}) is None:
    sys.exit(3)

version = ""
try:
    from importlib.metadata import version as _v
    version = _v({PACKAGE_DIST!r})
except Exception:
    pass

config_file = ""
try:
    from videocaptioner.cli.config import CONFIG_FILE
    config_file = str(CONFIG_FILE)
except Exception:
    pass

print(json.dumps({{
    "version": version,
    "config_file": config_file,
    "python_version": sys.version.split()[0],
    "executable": sys.executable,
}}, ensure_ascii=False))
"""


@dataclass(frozen=True)
class VcInstall:
    """一次探测的结果。

    Attributes:
        installed: 是否探测到可用的 VideoCaptioner。
        launcher: 可直接拼 argv 的前缀。可能是
            `("/path/to/python", "-m", "videocaptioner")`（能拿到解释器时），
            也可能是 `("/path/to/videocaptioner",)`（只找到可执行脚本时）。
        kind: 命中方式，见 `_candidates()` 的取名；未安装时是最后一个尝试的方式。
        root: 探测时用的 VideoCaptioner 根目录；不在候选路径里时为空串。
        version: 包版本，拿不到为空串。
        python_version: 目标解释器的 Python 版本，拿不到为空串。
        config_file: 配置文件落盘路径。**由 platformdirs 决定**，macOS 上是
            `~/Library/Application Support/videocaptioner/config.toml`，
            不是文档里写的 `~/.config/...`。
        config_exists: 该配置文件是否已存在（存在通常意味着用户配过 key）。
        ffmpeg_path: ffmpeg 的绝对路径，没找到为空串（没它转写必然失败）。
        detail: 未安装时的原因说明，直接展示给用户。
    """

    installed: bool = False
    launcher: Tuple[str, ...] = field(default_factory=tuple)
    kind: str = ""
    root: str = ""
    version: str = ""
    python_version: str = ""
    config_file: str = ""
    config_exists: bool = False
    ffmpeg_path: str = ""
    detail: str = ""

    @property
    def ready(self) -> bool:
        """能否真正开始转写：装了 VideoCaptioner 且 ffmpeg 可用。"""
        return self.installed and bool(self.ffmpeg_path)

    def as_payload(self) -> dict:
        """接口形态（字段名与 schemas 对齐）。"""
        return {
            "installed": self.installed,
            "ready": self.ready,
            "launcher": list(self.launcher),
            "kind": self.kind,
            "root": self.root,
            "version": self.version,
            "python_version": self.python_version,
            "config_file": self.config_file,
            "config_exists": self.config_exists,
            "ffmpeg_path": self.ffmpeg_path,
            "detail": self.detail,
        }


# --------------------------------------------------------------------------
# 候选可执行文件
# --------------------------------------------------------------------------


def _sibling_python(script: Path) -> Optional[Path]:
    """找出与某个脚本同目录的解释器（venv 里 python 与 console script 是邻居）。

    这样即使只从 PATH 上找到了 `videocaptioner`，也能顺着它找到能 `-m` 的
    解释器 —— 比直接调脚本更抗 PATH 与 shebang 的差异。
    """
    names = ("python.exe", "python3.exe") if os.name == "nt" else ("python3", "python")
    for name in names:
        candidate = script.parent / name
        if candidate.is_file():
            return candidate
    return None


def _search_dirs() -> List[Path]:
    """模糊匹配时扫的父目录：工具箱根 + 仓库根。

    VideoCaptioner 按约定放在工具箱根下（与本仓库平级），但也允许直接放进
    仓库里，两个位置都扫一遍。目录不存在的直接跳过。
    """
    dirs: List[Path] = []
    for base in (toolbox_root(), repo_root()):
        if base is not None and base.is_dir():
            dirs.append(base)
    return dirs


def looks_like_vc(root: Path) -> bool:
    """这个目录看起来是不是一份 VideoCaptioner 安装。

    宽松判定：有 `.venv`（官方一键脚本与本机源码安装的形态）、有
    `videocaptioner/` 包目录（源码树）、或有打包好的 exe 都算。目的是滤掉
    「碰巧也叫 VideoCaptioner* 的无关目录」，不是严格校验 —— 所以页面上
    手动指定目录时**不用它当门槛**（那正是这次探测不到的原因），只拿它
    生成一句提醒。
    """
    return (
        (root / ".venv").is_dir()
        or (root / PACKAGE_MODULE).is_dir()
        or (root / "VideoCaptioner.exe").is_file()
    )


def _discover_roots(search_dirs: Optional[List[Path]] = None) -> List[Path]:
    """扫出 `<搜索目录>/VideoCaptioner*` 里像 VideoCaptioner 的那些目录。

    为什么需要模糊匹配：从 GitHub 下载 zip 解压出来的目录名默认带后缀
    （`VideoCaptioner-master` / `-main` / 带版本号），而配置里的默认值只有
    `VideoCaptioner` 一个精确路径 —— 用户明明装了却探测不到，就是这么来的。

    Args:
        search_dirs: 要扫的父目录；不传则用 `_search_dirs()`。
            这个参数是给测试注入 tmp_path 用的，生产走默认。

    Returns:
        命中的目录，精确名 `VideoCaptioner` 排在前面，其余按名称字典序 ——
        多次探测的候选顺序必须确定，否则同一台机器上结果会飘。
    """
    bases = _search_dirs() if search_dirs is None else search_dirs
    found: List[Path] = []
    for base in bases:
        try:
            children = list(base.glob("VideoCaptioner*"))
        except OSError:
            # 目录权限不足或扫描中途被删，跳过这个父目录就好，不该让探测整个失败
            continue
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            if looks_like_vc(child):
                found.append(child)

    found.sort(key=lambda path: (path.name != "VideoCaptioner", path.name.lower()))
    return found


def vc_root_candidates() -> List[Tuple[Path, str]]:
    """探测会尝试的根目录，按优先级：(路径, 来源)。

    来源取值：
    - `explicit` —— 配置里显式指定的（`SUBTITLE_VC_ROOT`，含页面手动指定的）；
    - `discovered` —— 模糊匹配扫出来的。

    按 resolve 后的路径去重（Windows 上还要忽略大小写），否则「显式值正好等于
    模糊匹配结果」时会把同一套 venv 候选加两遍。
    """
    result: List[Tuple[Path, str]] = []
    seen: Set[str] = set()

    def push(path: Path, source: str) -> None:
        try:
            text = str(path.resolve())
        except OSError:
            text = str(path)
        # Windows 上路径大小写不敏感，不做 casefold 会把同一个目录算成两个。
        # 只碰 os.name（不碰 os.path）是为了留住测试里「换掉整个 os 替身来
        # 模拟另一个平台」的接缝。
        key = text.casefold() if os.name == "nt" else text
        if key in seen:
            return
        seen.add(key)
        result.append((path, source))

    configured = (settings.SUBTITLE_VC_ROOT or "").strip()
    if configured:
        push(Path(configured).expanduser(), "explicit")
    for path in _discover_roots():
        push(path, "discovered")

    return result


def _candidates() -> List[Tuple[List[str], str, str]]:
    """按优先级列出「可能的调用方式」：(launcher, kind, root)。

    顺序遵从「越确定越靠前」：
    1. 配置里显式指定的解释器（用户说了算）；
    2. 各候选根目录下的 venv —— 本机与官方一键脚本的装法。显式指定的根在前
       （kind 仍是 `venv-python` / `venv-script`），模糊匹配到的在后
       （kind 带 `discovered-` 前缀，便于诊断时看清是哪来的）；
    3. 后端自己的解释器 —— 覆盖「直接 pip install 到后端环境」这种最常见的情况，
       且这一步就地判断、不起子进程；
    4. PATH 上的 videocaptioner。

    Windows 与 POSIX 的差异全部收在 venv 的目录布局上（`Scripts/python.exe`
    对 `bin/python`），别处不再分平台。
    """
    candidates: List[Tuple[List[str], str, str]] = []
    configured_root = settings.SUBTITLE_VC_ROOT

    if settings.SUBTITLE_VC_PYTHON:
        # 先按「这是个解释器」试，再按「这是个可执行脚本」试 —— 用户填哪种都行
        override = str(Path(settings.SUBTITLE_VC_PYTHON).expanduser())
        candidates.append(([override, "-m", PACKAGE_MODULE], "override", configured_root))
        candidates.append(([override], "override-script", configured_root))

    for root_path, source in vc_root_candidates():
        root = str(root_path)
        # 显式指定的保持原来的 kind 字符串，模糊匹配的显式标出来源
        prefix = "" if source == "explicit" else f"{source}-"
        venv = root_path / ".venv"
        if os.name == "nt":
            candidates.append(([str(venv / "Scripts" / "python.exe"), "-m", PACKAGE_MODULE],
                               f"{prefix}venv-python", root))
            candidates.append(([str(venv / "Scripts" / f"{PACKAGE_MODULE}.exe")],
                               f"{prefix}venv-script", root))
        else:
            candidates.append(([str(venv / "bin" / "python"), "-m", PACKAGE_MODULE],
                               f"{prefix}venv-python", root))
            candidates.append(([str(venv / "bin" / PACKAGE_MODULE)], f"{prefix}venv-script",
                               root))

    # 后端环境里就地判断，命中才去验证（find_spec 不导入包，也不产生副作用）
    if _module_available_locally():
        candidates.append(([sys.executable, "-m", PACKAGE_MODULE], "backend-python", ""))

    script = find_tool(PACKAGE_MODULE)
    if script:
        interpreter = _sibling_python(Path(script))
        if interpreter is not None:
            candidates.append(([str(interpreter), "-m", PACKAGE_MODULE], "path", ""))
        candidates.append(([script], "path-script", ""))

    return candidates


def _module_available_locally() -> bool:
    """后端自己的解释器里能不能 import 到 VideoCaptioner。

    `find_spec` 只查定位器、不真正导入，所以不会在 HOME 下建配置目录 ——
    这一步必须保持零副作用，它每次调用都会跑。
    """
    try:
        import importlib.util

        return importlib.util.find_spec(PACKAGE_MODULE) is not None
    except (ImportError, ValueError):
        return False


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------


def _probe(launcher: List[str]) -> Optional[dict]:
    """验证一条调用方式：跑一次目标程序，拿版本（能拿到的话还有配置路径）。

    探测命令取决于 launcher 的形态 —— 这一点必须分清楚：
    - `[解释器, "-m", "videocaptioner"]`：探测要问的是**解释器**（`-c` 跑小脚本），
      不能把 `-m videocaptioner` 一起带上，那是待会儿执行转写时才用的；
    - `[可执行脚本]`：拿不到解释器，只能问它自己的 `--version`，重一点但没办法。

    Returns:
        解析成功的字典；可执行文件不存在、超时、包不在里面、输出看不懂都返回
        None（调用方继续试下一条候选）。
    """
    if len(launcher) > 1:
        argv = [launcher[0], "-c", _PROBE_SCRIPT]
        parser = _parse_probe_json
    else:
        argv = [launcher[0], "--version"]
        parser = _parse_version_text

    result = run_probe(argv, timeout=PROBE_TIMEOUT_SECONDS)
    if result is None:
        logger.debug("探测 VideoCaptioner 失败 | %s", launcher[0])
        return None
    if result.returncode != 0:
        return None
    # run_probe 已经按字节捕获并解码（子进程可能吐与父进程代码页不一致的
    # 字节，text=True 会在建读线程里炸）—— 探测看不懂输出顶多换下一条候选
    return parser(result.stdout)


def _parse_probe_json(stdout: str) -> Optional[dict]:
    """从探测脚本的输出里取最后一行 JSON。

    取最后一行而不是第一行：导入包时可能有警告打在前面，真正的那行在末尾。
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _parse_version_text(stdout: str) -> Optional[dict]:
    """把 `videocaptioner 1.4.2` 这样的版本输出解析成探测结果（拿不到配置路径）。"""
    for line in stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == PACKAGE_MODULE:
            return {"version": parts[1], "config_file": ""}
    return None


_cache_lock = threading.Lock()
_cache: Optional[Tuple[float, VcInstall]] = None


def detect(*, force: bool = False) -> VcInstall:
    """探测 VideoCaptioner，返回可用的调用方式（结果带缓存）。

    Args:
        force: 忽略缓存重新探测（前端「重新检测」按钮用；用户刚装完时
            缓存里的结论已经过期了）。

    Returns:
        探测结果。**任何失败都不抛异常** —— 没装 VideoCaptioner 是这个功能
        的正常状态之一，页面要据此显示安装指引，不该变成一个 500。
    """
    global _cache

    ttl = settings.SUBTITLE_DETECT_CACHE_SECONDS
    now = time.monotonic()
    if not force and _cache is not None:
        cached_at, cached = _cache
        if now - cached_at < ttl:
            return cached

    install = _detect_uncached()
    with _cache_lock:
        _cache = (now, install)
    return install


def _detect_uncached() -> VcInstall:
    """真正跑一遍探测：逐条候选试到第一个通的。"""
    candidates = _candidates()
    last_kind = candidates[-1][1] if candidates else ""
    last_root = candidates[-1][2] if candidates else ""

    for launcher, kind, root in candidates:
        payload = _probe(launcher)
        if payload is None:
            continue

        install = VcInstall(
            installed=True,
            launcher=tuple(launcher),
            kind=kind,
            root=root,
            version=str(payload.get("version") or ""),
            python_version=str(payload.get("python_version") or ""),
            config_file=str(payload.get("config_file") or ""),
            ffmpeg_path=find_tool("ffmpeg") or "",
        )
        install = _with_config_exists(install)
        logger.info(
            "探测到 VideoCaptioner | kind=%s | 版本=%s | ffmpeg=%s",
            kind,
            install.version or "未知",
            install.ffmpeg_path or "未找到",
        )
        return install

    # 一条都没通：给用户一句能自己判断的说明，而不是「未知错误」。
    # 这里要列出**所有**找过的根目录，不能只说最后一个 —— 多根候选下只说
    # 最后一个，用户会以为自己装的那个位置根本没被找过。
    searched = [str(root) for root, _source in vc_root_candidates()]
    root_hint = f"（已查找 {'、'.join(searched)}）" if searched else ""
    logger.info(
        "未探测到可用的 VideoCaptioner | 最后尝试=%s | 已查找=%s",
        last_kind or "无候选",
        "、".join(searched) or "无候选根目录",
    )
    return VcInstall(
        installed=False,
        kind=last_kind,
        root=last_root,
        ffmpeg_path=find_tool("ffmpeg") or "",
        detail=(
            f"没有找到可用的 VideoCaptioner{root_hint}。"
            "已经装了却探测不到时，多半是目录名不一样（比如从 GitHub 下载解压出来的"
            "带 -master 后缀）—— 可以在本页手动指定它的目录。"
            "也可以按下面的命令安装，或把 SUBTITLE_VC_PYTHON 指到已有的解释器。"
        ),
    )


def _with_config_exists(install: VcInstall) -> VcInstall:
    """补一个「配置文件是否已存在」的探测结果（dataclass 冻结，只能重建）。"""
    if not install.config_file:
        return install
    try:
        exists = Path(install.config_file).is_file()
    except OSError:
        exists = False
    return replace(install, config_exists=exists)


def reset_cache() -> None:
    """清空缓存（测试用；生产走 detect(force=True)）。"""
    global _cache
    with _cache_lock:
        _cache = None


# --------------------------------------------------------------------------
# 安装指引
# --------------------------------------------------------------------------

RELEASES_URL = "https://github.com/WEIFENG2333/VideoCaptioner/releases"
RUN_SH_URL = (
    "https://raw.githubusercontent.com/WEIFENG2333/VideoCaptioner/master/scripts/run.sh"
)
PYPI_URL = "https://pypi.org/project/videocaptioner/"


def platform_key() -> str:
    """当前系统的简写：macos / windows / linux。

    探测跑在后端，而这是个本机工具（后端与浏览器在同一台机器），所以后端
    的系统就是用户要照着装的那个系统 —— 指引只需要出当前系统这一份。
    """
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def platform_label() -> str:
    """给用户看的系统名。"""
    return {"macos": "macOS", "windows": "Windows", "linux": "Linux"}[platform_key()]


def install_hints(install: Optional[VcInstall] = None) -> List[dict]:
    """按当前系统产出安装指引（只出当前系统那一份）。

    每条都是一个可复制的命令或一个可点开的链接；前端只负责渲染与复制，
    不在这里做平台判断。
    """
    key = platform_key()
    hints: List[dict] = []

    if key == "windows":
        hints.append(
            {
                "title": "安装 VideoCaptioner（命令行版）",
                "command": "py -3.11 -m pip install videocaptioner",
                "note": "需要 Python 3.10–3.12；装到哪个解释器，后端就调哪个",
                "url": PYPI_URL,
            }
        )
        hints.append(
            {
                "title": "或安装图形界面版",
                "command": "",
                "note": "从 GitHub Release 下载安装包，双击安装",
                "url": RELEASES_URL,
            }
        )
        hints.append(
            {
                "title": "安装 ffmpeg（必需）",
                "command": "winget install Gyan.FFmpeg",
                "note": "转写前要把视频转成音频；装完确认 ffmpeg 在 PATH 里",
                "url": "",
            }
        )
    elif key == "macos":
        hints.append(
            {
                "title": "安装 VideoCaptioner（命令行版）",
                "command": "python3 -m pip install videocaptioner",
                "note": "需要 Python 3.10–3.12；装到哪个解释器，后端就调哪个",
                "url": PYPI_URL,
            }
        )
        hints.append(
            {
                "title": "或安装图形界面版",
                "command": f"curl -fsSL {RUN_SH_URL} | bash",
                "note": "官方一键脚本（装的是图形界面版；命令行走上面那条 pip）",
                "url": RELEASES_URL,
            }
        )
        hints.append(
            {
                "title": "安装 ffmpeg（必需）",
                "command": "brew install ffmpeg",
                "note": "转写前要把视频转成音频；装完确认 ffmpeg 在 PATH 里",
                "url": "",
            }
        )
    else:
        hints.append(
            {
                "title": "安装 VideoCaptioner（命令行版）",
                "command": "python3 -m pip install videocaptioner",
                "note": "需要 Python 3.10–3.12",
                "url": PYPI_URL,
            }
        )
        hints.append(
            {
                "title": "安装 ffmpeg（必需）",
                "command": "sudo apt install ffmpeg",
                "note": "或按发行版的包管理器安装",
                "url": "",
            }
        )

    hints.append(
        {
            "title": "装好后回到本页",
            "command": "",
            "note": "点「重新检测」即可，不需要重启服务",
            "url": "",
        }
    )
    return hints


def vc_search_dir() -> str:
    """模糊匹配扫描的首选父目录（工具箱根），给前端目录选择器当初始位置。

    空串表示推不出（仓库被单独拷出来、那一层不存在），前端会退到默认目录。
    """
    dirs = _search_dirs()
    return str(dirs[0]) if dirs else ""


def probe_environment(*, refresh: bool = False) -> dict:
    """组装环境自检结果，直接给 `/subtitle/environment` 用。

    Args:
        refresh: 绕过探测缓存重新检测。

    Returns:
        与 `SubtitleEnvironmentResponse` 字段一一对应的字典。
        **新增键时务必同步加到 schemas/subtitle_job.py 的同名模型上** ——
        FastAPI 的 response_model 会把模型里没有的键静默丢掉，前端拿不到
        还查不出原因。
    """
    install = detect(force=refresh)
    warning = shadowing_warning()
    return {
        **install.as_payload(),
        "platform": platform_key(),
        "platform_label": platform_label(),
        "python_platform": platform.platform(),
        "materials_dir": str(materials_root()),
        "default_input_dir": str(subdir(SOURCE)),
        "default_output_dir": str(subdir(SUBTITLE)),
        "install_hints": install_hints(install),
        "vc_root_source": vc_root_source(),
        "vc_search_dir": vc_search_dir(),
        "warnings": [warning] if warning else [],
    }
