"""
环境探测模块。

职责：找出两个工具各自的 Python 解释器，以及 ffmpeg / ffprobe 的位置，
并把结果包装成 Probe 对象，附带「缺失时该怎么办」的中文修复建议。

设计说明：
* 探测结果按进程缓存（本进程内只算一次），因为交互菜单会反复调用。
* 所有探测函数都不抛异常，失败时返回 ok=False 的 Probe，由调用方决定怎么提示。
* 支持用环境变量覆盖解释器路径，方便用户换环境：
      VCT_VC_PYTHON   指定 VideoCaptioner 的 python
      VCT_VSR_PYTHON  指定 video-subtitle-remover 的 python
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# 目录常量
# --------------------------------------------------------------------------

# 工具箱根目录 = 本文件所在包的上一层（即「内容制作工具/」）
TOOLBOX_ROOT = Path(__file__).resolve().parent.parent

VC_ROOT = TOOLBOX_ROOT / "VideoCaptioner"
VSR_ROOT = TOOLBOX_ROOT / "video-subtitle-remover-main"
FFMPEG_BIN_DIR = TOOLBOX_ROOT / "ffmpeg-bin"

# 运行日志与缓存目录
VCT_HOME = TOOLBOX_ROOT / ".vct"
LOG_DIR = VCT_HOME / "logs"

# video-subtitle-remover 在 README 里推荐的虚拟环境名，
# 用户可能用 conda 建在别处，这里列出常见位置作为回退候选。
VSR_CONDA_CANDIDATES = [
    Path.home() / "miniconda3" / "envs" / "videoEnv",
    Path.home() / "anaconda3" / "envs" / "videoEnv",
    Path.home() / ".conda" / "envs" / "videoEnv",
    Path.home() / "miniconda3" / "envs" / "vsr",
    Path.home() / "anaconda3" / "envs" / "vsr",
]

# 安装 VSR 环境的脚本
SETUP_VSR_SCRIPT = TOOLBOX_ROOT / "scripts" / "setup-vsr.sh"


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class Probe:
    """一次探测的结果。

    Attributes:
        name:    探测项的中文名称，用于打印。
        ok:      是否可用。
        detail:  可用时的说明（版本、路径等）；不可用时的原因。
        fix:     不可用时的修复建议（可直接照抄的命令）。
        path:    可用时的可执行文件路径。
        extra:   额外信息，供 doctor 之类的地方展示。
    """

    name: str
    ok: bool
    detail: str
    fix: str = ""
    path: Path | None = None
    extra: dict = field(default_factory=dict)


# 进程内缓存：模块级字典，键为探测项名称
_CACHE: dict[str, Probe] = {}


def _cached(key: str, builder) -> Probe:
    """带缓存的探测包装。builder 是一个无参函数，返回 Probe。"""
    if key not in _CACHE:
        try:
            _CACHE[key] = builder()
        except Exception as exc:  # 探测本身绝不应该炸掉整个 CLI
            _CACHE[key] = Probe(name=key, ok=False, detail=f"探测失败：{exc}")
    return _CACHE[key]


def clear_cache() -> None:
    """清空探测缓存。安装完环境后调用，避免还拿着旧结果。"""
    _CACHE.clear()


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------


def _python_version(python: Path) -> str | None:
    """取某个解释器的版本号字符串，例如 '3.11.11'。失败返回 None。"""
    try:
        out = subprocess.run(
            [str(python), "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _module_exists(python: Path, module: str) -> bool:
    """检查某个解释器里能否 import 指定模块。"""
    try:
        out = subprocess.run(
            [str(python), "-c", f"import {module}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def _first_existing(pairs: list[tuple[Path, str]]) -> Probe | None:
    """在一组候选路径里找第一个真实存在的 python 解释器。

    Args:
        pairs: [(候选解释器路径, 来源说明), ...]
    Returns:
        找到则返回 Probe，都不存在则返回 None。
    """
    for python, source in pairs:
        if python.exists() and os.access(python, os.X_OK):
            version = _python_version(python)
            if version is None:
                continue
            return Probe(
                name="python",
                ok=True,
                detail=f"{python}（Python {version}，{source}）",
                path=python,
                extra={"version": version, "source": source},
            )
    return None


# --------------------------------------------------------------------------
# VideoCaptioner
# --------------------------------------------------------------------------


def probe_videocaptioner(refresh: bool = False) -> Probe:
    """探测 VideoCaptioner 的可用性。

    优先使用工具自带的 .venv，其次使用 PATH 上的 videocaptioner 命令。
    返回的 Probe.path 指向 python 解释器；若走的是 PATH 上的命令，
    则 path 指向该命令，extra['mode'] 为 'command'。
    """
    if refresh:
        _CACHE.pop("videocaptioner", None)

    def build() -> Probe:
        if not VC_ROOT.exists():
            return Probe(
                name="VideoCaptioner",
                ok=False,
                detail=f"未找到目录 {VC_ROOT}",
                fix="确认工具箱目录结构完整，或把 VideoCaptioner 放回该位置。",
            )

        # 1) 优先用自带的虚拟环境
        override = os.environ.get("VCT_VC_PYTHON")
        candidates: list[tuple[Path, str]] = []
        if override:
            candidates.append((Path(override).expanduser(), "来自环境变量 VCT_VC_PYTHON"))
        candidates.append((VC_ROOT / ".venv" / "bin" / "python", "工具箱自带虚拟环境"))

        found = _first_existing(candidates)
        if found is not None:
            # 确认 videocaptioner 包真的装在这个环境里
            if not _module_exists(found.path, "videocaptioner"):
                return Probe(
                    name="VideoCaptioner",
                    ok=False,
                    detail=f"{found.path} 里没有安装 videocaptioner 包",
                    fix=(
                        f"重新安装：\n"
                        f"    {VC_ROOT}/.venv/bin/python -m pip install -e {VC_ROOT}"
                    ),
                    path=found.path,
                )
            found.name = "VideoCaptioner"
            found.detail = f"环境就绪 — {found.detail}"
            found.extra["mode"] = "venv"
            return found

        # 2) 回退到 PATH 上的命令
        cmd = shutil.which("videocaptioner")
        if cmd:
            return Probe(
                name="VideoCaptioner",
                ok=True,
                detail=f"环境就绪 — 使用 PATH 上的命令 {cmd}",
                path=Path(cmd),
                extra={"mode": "command"},
            )

        return Probe(
            name="VideoCaptioner",
            ok=False,
            detail="没找到可用的 Python 环境（既无 .venv，PATH 上也没有 videocaptioner）",
            fix=(
                f"建立虚拟环境并安装：\n"
                f"    cd {VC_ROOT}\n"
                f"    python3.11 -m venv .venv\n"
                f"    .venv/bin/python -m pip install -e ."
            ),
        )

    return _cached("videocaptioner", build)


def videocaptioner_command() -> list[str]:
    """返回调用 VideoCaptioner CLI 的命令前缀。

    Raises:
        RuntimeError: 环境不可用时抛出，调用方应先检查 probe 结果。
    """
    probe = probe_videocaptioner()
    if not probe.ok or probe.path is None:
        raise RuntimeError("VideoCaptioner 环境不可用：" + probe.detail)

    if probe.extra.get("mode") == "command":
        return [str(probe.path)]
    return [str(probe.path), "-m", "videocaptioner.cli.main"]


# --------------------------------------------------------------------------
# video-subtitle-remover
# --------------------------------------------------------------------------


def probe_vsr(refresh: bool = False) -> Probe:
    """探测 video-subtitle-remover 的可用性。

    注意：这个工具的 backend/config.py 顶部就 import qfluentwidgets（PySide6 系），
    所以即使是命令行模式也依赖 GUI 库，环境必须按 requirements.txt 全量安装。
    这里会顺带检查几个关键依赖，缺失时说明装到哪一步了。
    """
    if refresh:
        _CACHE.pop("vsr", None)

    def build() -> Probe:
        if not VSR_ROOT.exists():
            return Probe(
                name="video-subtitle-remover",
                ok=False,
                detail=f"未找到目录 {VSR_ROOT}",
                fix="确认工具箱目录结构完整，或把 video-subtitle-remover-main 放回该位置。",
            )

        override = os.environ.get("VCT_VSR_PYTHON")
        candidates: list[tuple[Path, str]] = []
        if override:
            candidates.append((Path(override).expanduser(), "来自环境变量 VCT_VSR_PYTHON"))

        # 就地建在 VSR 目录下的环境，两种常见命名都认。
        # 注意 README 里 conda 环境默认叫 videoEnv，很多人会直接 `python -m venv videoEnv`
        # 建在仓库里，所以这里必须一并覆盖，否则装好了也认不出来。
        candidates.append((VSR_ROOT / "videoEnv" / "bin" / "python", "VSR 目录下的 videoEnv"))
        candidates.append((VSR_ROOT / ".venv" / "bin" / "python", "工具箱内虚拟环境"))
        for env_dir in VSR_CONDA_CANDIDATES:
            candidates.append((env_dir / "bin" / "python", f"conda 环境 {env_dir.name}"))

        found = _first_existing(candidates)
        setup_hint = (
            f"运行一键安装脚本：\n"
            f"    bash {SETUP_VSR_SCRIPT}\n"
            f"（约 2-4GB 下载，耗时 10-20 分钟）\n"
            f"也可以手动指定已有环境：\n"
            f"    export VCT_VSR_PYTHON=/你的环境/bin/python"
        )

        if found is None:
            return Probe(
                name="video-subtitle-remover",
                ok=False,
                detail="没找到可用的 Python 环境（源码和模型都在，只差运行环境）",
                fix=setup_hint,
            )

        # 关键依赖检查。torch 用于重绘模型，paddleocr 用于字幕文本检测，
        # qfluentwidgets 是 backend/config.py 的硬依赖。
        missing = []
        for module, label in (
            ("torch", "torch"),
            ("paddleocr", "paddleocr"),
            ("qfluentwidgets", "PySide6 系 GUI 库（qfluentwidgets）"),
            ("cv2", "opencv-python"),
        ):
            if not _module_exists(found.path, module):
                missing.append(label)

        if missing:
            return Probe(
                name="video-subtitle-remover",
                ok=False,
                detail=(
                    f"找到解释器 {found.path}（Python {found.extra['version']}），"
                    f"但缺少依赖：{'、'.join(missing)}"
                ),
                fix=setup_hint,
                path=found.path,
            )

        found.name = "video-subtitle-remover"
        found.detail = f"环境就绪 — {found.detail}"
        found.extra["mode"] = "venv"
        return found

    return _cached("vsr", build)


def vsr_command(allow_missing: bool = False) -> tuple[list[str], Path]:
    """返回调用 VSR 命令行的 (命令前缀, 工作目录)。

    VSR 必须在自己的根目录下运行：backend/config.py 里
    CONFIG_FILE = 'config/config.json' 是相对当前工作目录的路径。

    Args:
        allow_missing: 环境不可用时是否仍返回一个占位命令。
            这是给 dry-run 用的 —— 用户想在装环境之前先看看命令长什么样，
            这时用看得懂的占位符代替解释器路径，比直接报错更有用。
    Raises:
        RuntimeError: 环境不可用且 allow_missing 为 False 时抛出。
    """
    probe = probe_vsr()
    if not probe.ok or probe.path is None:
        if allow_missing:
            return ["<video-subtitle-remover 的 python>", "backend/main.py"], VSR_ROOT
        raise RuntimeError("video-subtitle-remover 环境不可用：" + probe.detail)
    return [str(probe.path), "backend/main.py"], VSR_ROOT


# --------------------------------------------------------------------------
# ffmpeg
# --------------------------------------------------------------------------


def probe_ffmpeg(refresh: bool = False) -> Probe:
    """探测 ffmpeg / ffprobe。VideoCaptioner 的合成与下载都依赖它们。"""
    if refresh:
        _CACHE.pop("ffmpeg", None)

    def build() -> Probe:
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")

        if ffmpeg and ffprobe:
            version = ""
            try:
                out = subprocess.run(
                    [ffmpeg, "-version"], capture_output=True, text=True, timeout=20
                )
                if out.returncode == 0 and out.stdout:
                    version = out.stdout.splitlines()[0][:80]
            except (OSError, subprocess.SubprocessError):
                pass
            return Probe(
                name="ffmpeg",
                ok=True,
                detail=f"可用 — {ffmpeg}" + (f"（{version}）" if version else ""),
                path=Path(ffmpeg),
                extra={"ffprobe": ffprobe},
            )

        missing = [n for n, p in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not p]
        fix_lines = [f"缺少：{'、'.join(missing)}"]
        if FFMPEG_BIN_DIR.exists():
            fix_lines.append(f"工具箱里已备好二进制（{FFMPEG_BIN_DIR}），可以：")
            fix_lines.append(f"    export PATH=\"{FFMPEG_BIN_DIR}:$PATH\"")
        fix_lines.append("或直接安装：")
        fix_lines.append("    brew install ffmpeg")

        return Probe(
            name="ffmpeg",
            ok=False,
            detail="PATH 上找不到 ffmpeg / ffprobe",
            fix="\n".join(fix_lines),
        )

    return _cached("ffmpeg", build)


# --------------------------------------------------------------------------
# PySceneDetect
# --------------------------------------------------------------------------


def probe_scenedetect(refresh: bool = False) -> Probe:
    """探测 PySceneDetect（scenedetect 命令）。vct scene 的镜头分割依赖它。

    它是**独立安装的 CLI 工具**，不装进 vct 自己的解释器，也不装进
    VideoCaptioner 的虚拟环境 —— 和 ffmpeg 一样，vct 只负责找到它并调用。
    """
    if refresh:
        _CACHE.pop("scenedetect", None)

    def build() -> Probe:
        found = shutil.which("scenedetect")
        from_where = "PATH"
        if not found:
            # uv tool install 默认装到 ~/.local/bin，而它不一定在 PATH 上。
            # 这里额外探一下默认位置，免得用户明明装了却被告知缺失。
            fallback = Path.home() / ".local" / "bin" / "scenedetect"
            if fallback.is_file() and os.access(fallback, os.X_OK):
                found = str(fallback)
                from_where = "uv tool 默认目录"

        if not found:
            return Probe(
                name="PySceneDetect",
                ok=False,
                detail="PATH 上找不到 scenedetect 命令",
                fix=(
                    "镜头分割用 PySceneDetect。它是个独立工具，"
                    "不会动现有的 Python 环境：\n"
                    "    uv tool install scenedetect\n"
                    "装完若仍提示找不到，把它加进 PATH：\n"
                    '    export PATH="$HOME/.local/bin:$PATH"'
                ),
            )

        # 注意：scenedetect 没有 --version 选项（会被当成参数错误退出），
        # 版本要跑 version 子命令，从首行 "[PySceneDetect] PySceneDetect 0.7.1" 里取。
        version = ""
        try:
            out = subprocess.run(
                [found, "version"], capture_output=True, text=True, timeout=60
            )
            if out.returncode == 0 and out.stdout:
                first_line = out.stdout.splitlines()[0]
                if "PySceneDetect" in first_line:
                    version = first_line.split("PySceneDetect")[-1].strip()
        except (OSError, subprocess.SubprocessError):
            pass

        detail = f"可用 — {found}" + (f"（v{version}）" if version else "")
        if from_where != "PATH":
            detail += f" [{from_where}，不在 PATH 上]"
        return Probe(name="PySceneDetect", ok=True, detail=detail, path=Path(found))

    return _cached("scenedetect", build)


def probe_questionary(refresh: bool = False) -> Probe:
    """探测方向键交互所用的 questionary。

    这属于**软依赖**：缺了只影响交互体验（菜单退化成输入编号），
    不影响任何处理功能，所以 Probe 带 extra["soft"]=True 标记，
    由 doctor 决定不计入退出码。
    """
    if refresh:
        _CACHE.pop("questionary", None)

    def build() -> Probe:
        # 直接 import：doctor 是低频命令，这点开销无所谓。
        # 菜单那条路径走的是 ui._load_questionary 的惰性缓存，互不影响。
        try:
            import questionary
        except Exception:  # noqa: BLE001 —— 装坏了也只该降级，不该报错
            return Probe(
                name="交互界面（questionary）",
                ok=False,
                detail="未安装 —— 菜单会退化成输入编号的方式",
                fix=(
                    "装上它，菜单就能用方向键选择（只影响交互体验，不影响处理功能）：\n"
                    f"    {sys.executable} -m pip install questionary"
                ),
                extra={"soft": True},
            )
        return Probe(
            name="交互界面（questionary）",
            ok=True,
            detail=f"v{questionary.__version__} — 菜单支持方向键选择",
            extra={"soft": True},
        )

    return _cached("questionary", build)


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------


def probe_all(refresh: bool = False) -> list[Probe]:
    """一次性探测全部项目，供 doctor 和交互菜单使用。"""
    if refresh:
        clear_cache()
    return [
        probe_videocaptioner(),
        probe_vsr(),
        probe_ffmpeg(),
        probe_scenedetect(),
        probe_questionary(),
    ]


def python_runtime_ok() -> Probe:
    """检查运行 vct 本身的 Python 是否满足要求（需要 3.10+）。"""
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 10)
    return Probe(
        name="vct 运行环境",
        ok=ok,
        detail=f"Python {major}.{minor}.{sys.version_info[2]} — {sys.executable}",
        fix="vct 需要 Python 3.10 或更高版本。" if not ok else "",
    )
