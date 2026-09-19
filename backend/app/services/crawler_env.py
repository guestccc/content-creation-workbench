"""MediaCrawler 的探测与环境自检。

素材抓取的实际执行者是 **MediaCrawler**（不在本仓库里，默认放在工具箱根下
与本仓库平级，见 `app/core/config.py` 的 CRAWL_MC_ROOT）。这个模块回答：

1. **它装了吗？能用哪个解释器调？** —— 探测出一条能直接拼 argv 的调用前缀；
2. **周边环境什么样？** —— Node.js（抖音/知乎签名要）、各平台登录态缓存、
   媒体下载开关、知乎 creator 补丁状态；
3. **没装的话让用户怎么装？** —— 分步安装指引（只是文本，绝不替用户执行）。

解释器优先级（每级都要跑一次探活，探不过就降级）：
1. `<MC>/.venv` 里的 python —— `uv sync` 的产物，固定、启动快，而且**绕开
   uv 在非 ASCII 路径下的兼容问题**（本仓库路径含中文，已经踩过 uv 的坑）；
2. PATH 上的 uv（`uv run python`）—— 能用，但每次执行都要做一次依赖解析，
   且 uv 对中文 cwd 的行为不确定，命中这一级时会在 warnings 里提醒。

探测成本与副作用：python 探活约 0.1 秒、node --version 约 0.1 秒、
browser_data 扫目录一次 —— 都不算贵但也没必要每请求都做，结果带 TTL 缓存
（前端「重新检测」按钮走 force=True 绕过）。
"""

import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from app.core.config import settings
from app.core.logging import get_logger
from app.core.materials import CRAWL, subdir
from app.models.crawl_job import CrawlPlatform
from app.services.media_tools import find_tool, run_probe

logger = get_logger(__name__)

#: 探测子进程的超时（秒）。
PROBE_TIMEOUT_SECONDS = 10

#: venv 的解释器相对位置（Windows 与 POSIX 布局不同，收在这两处）。
def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


@dataclass(frozen=True)
class McInstall:
    """一次探测的结果。

    Attributes:
        installed: 是否探测到可用的 MediaCrawler（根目录存在 + 解释器探活通过）。
        launcher: 可直接拼 argv 的前缀。形如 `("<venv>/python.exe")` 或
            `("uv", "run", "python")` —— runner 会在后面接 ["main.py", ...]。
        kind: 命中方式：venv-python / uv-run；未安装为空串。
        mc_root: 探测时使用的 MC 根目录。
        python_version: 目标解释器的 Python 版本，拿不到为空串。
        detail: 未安装时的原因说明，直接展示给用户。
    """

    installed: bool = False
    launcher: Tuple[str, ...] = field(default_factory=tuple)
    kind: str = ""
    mc_root: str = ""
    python_version: str = ""
    detail: str = ""


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------


def looks_like_mc(root: Path) -> bool:
    """这个目录看起来是不是一份 MediaCrawler 仓库。

    宽松判定：有 main.py（CLI 入口）+ config 目录就认。目的是滤掉「碰巧
    同名的无关目录」，不做严格校验。
    """
    return (root / "main.py").is_file() and (root / "config").is_dir()


def _probe_python(launcher: List[str]) -> Optional[str]:
    """跑一次 `python --version` 验证解释器能用，返回版本号（如 3.11.16）。

    注意问的是解释器本身：launcher 的 uv 形态是 ("uv", "run", "python")，
    `--version` 会被传给 python 而不是 uv，两种形态可以用同一套探测。
    """
    argv = list(launcher) + ["--version"]
    result = run_probe(
        argv,
        timeout=PROBE_TIMEOUT_SECONDS,
        cwd=str(Path(settings.CRAWL_MC_ROOT).expanduser())
        if Path(settings.CRAWL_MC_ROOT).expanduser().is_dir()
        else None,
    )
    if result is None:
        logger.debug("探测 MediaCrawler 解释器失败 | %s", launcher)
        return None

    if result.returncode != 0:
        return None
    # `Python 3.11.16` —— 取以 Python 开头那行的最后一段
    for line in (result.stdout + result.stderr).splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == "Python":
            return parts[1]
    return None


_cache_lock = threading.Lock()
_cache: Optional[Tuple[float, dict]] = None


def detect(*, force: bool = False) -> McInstall:
    """探测 MediaCrawler 的可调用方式（结果带缓存）。

    Returns:
        探测结果。**任何失败都不抛异常** —— 没装 MC 是这个功能的正常状态
        之一，页面要据此显示安装指引，不该变成 500。
    """
    payload = _cached_payload(force=force)
    return payload["install"]


def _cached_payload(*, force: bool) -> dict:
    """带 TTL 缓存的完整环境探测（install + 周边环境）。

    环境自检接口需要全部信息，而 runner 只需要 install —— 用一个缓存
    装整份，两种调用方都只探测一次。
    """
    global _cache

    ttl = settings.CRAWL_ENV_CACHE_SECONDS
    now = time.monotonic()
    if not force and _cache is not None:
        cached_at, cached = _cache
        if now - cached_at < ttl:
            return cached

    payload = _detect_uncached()
    with _cache_lock:
        _cache = (now, payload)
    return payload


def _detect_uncached() -> dict:
    """真正跑一遍探测：解释器降级链 + 周边环境。"""
    root = Path(settings.CRAWL_MC_ROOT).expanduser()
    warnings: List[str] = []

    install = McInstall(mc_root=str(root))

    if not root.is_dir():
        install = McInstall(
            mc_root=str(root),
            detail=f"目录不存在：{root}。请把 MediaCrawler 仓库放在工具箱根目录下，"
            "或修改 backend/.env 里的 CRAWL_MC_ROOT。",
        )
    elif not looks_like_mc(root):
        install = McInstall(
            mc_root=str(root),
            detail=f"目录里没有 main.py / config/，看起来不是 MediaCrawler 仓库：{root}",
        )
    else:
        # 1. 仓库自带 venv —— 首选（绕开 uv 的中文路径兼容问题）
        venv = _venv_python(root)
        if venv.is_file():
            version = _probe_python([str(venv)])
            if version:
                install = McInstall(
                    installed=True,
                    launcher=(str(venv),),
                    kind="venv-python",
                    mc_root=str(root),
                    python_version=version,
                )
        # 2. PATH 上的 uv —— 降级路径
        if not install.installed:
            uv = find_tool("uv")
            if uv:
                version = _probe_python(["uv", "run", "python"])
                if version:
                    install = McInstall(
                        installed=True,
                        launcher=("uv", "run", "python"),
                        kind="uv-run",
                        mc_root=str(root),
                        python_version=version,
                    )
                    warnings.append(
                        "正在用 PATH 上的 uv 调用 MediaCrawler（仓库里没有可用的 .venv）。"
                        "uv 在含中文的路径下可能有兼容问题，建议在 MediaCrawler 目录里"
                        "执行 `uv sync` 生成 .venv 后重新检测。"
                    )

        if not install.installed:
            install = McInstall(
                mc_root=str(root),
                detail=(
                    "找到了 MediaCrawler 目录，但没有可用的解释器：仓库里没有 .venv，"
                    "PATH 上也没有 uv。请在 MediaCrawler 目录里执行 `uv sync` 安装依赖。"
                ),
            )

    if install.installed:
        logger.info(
            "探测到 MediaCrawler | kind=%s | python=%s | root=%s",
            install.kind,
            install.python_version or "未知",
            install.mc_root,
        )
    else:
        logger.info("未探测到可用的 MediaCrawler | %s", install.detail)

    return {
        "install": install,
        "node_version": _detect_node(),
        "login_states": scan_login_states(),
        "media_enabled": _detect_media_enabled(Path(install.mc_root)),
        "zhihu_creator_cli_supported": _detect_zhihu_creator_patch(Path(install.mc_root)),
        "warnings": warnings,
    }


def reset_cache() -> None:
    """清空缓存（测试用；生产走 _cached_payload(force=True)）。"""
    global _cache
    with _cache_lock:
        _cache = None


# --------------------------------------------------------------------------
# 周边环境
# --------------------------------------------------------------------------


def _detect_node() -> str:
    """探测 Node.js 版本（只有抖音/知乎的签名需要它），失败返回空串。"""
    node = find_tool("node")
    if not node:
        return ""
    # 按字节捕获再自己解码：node 的输出通常干净，但探测绝不能因为代码页
    # 不一致就崩（同一坑在 media_tools.decode_output 里写清楚了）
    result = run_probe([node, "--version"], timeout=PROBE_TIMEOUT_SECONDS)
    if result is None or result.returncode != 0:
        return ""
    # `v18.20.4` —— 去掉 v 前缀
    return result.stdout.strip().lstrip("v")


def scan_login_states(mc_root: Optional[Path] = None) -> List[dict]:
    """扫描 MC 的 browser_data/，报告各平台的登录态缓存情况。

    MC 的 SAVE_LOGIN_STATE=True 会把登录浏览器 profile 存在
    `<MC>/browser_data/{platform}_user_data_dir`（标准模式）与
    `cdp_{platform}_user_data_dir`（CDP 模式，默认启用）。页面用它显示
    「哪些平台已扫码过、可以直接抓」—— 用户不用挨个试。

    Returns:
        [{"platform", "platform_label", "cdp", "standard", "updated_at"}]，
        只返回至少有一种登录态的平台。
    """
    root = mc_root if mc_root is not None else Path(settings.CRAWL_MC_ROOT).expanduser()
    browser_data = root / "browser_data"
    states: List[dict] = []
    try:
        if not browser_data.is_dir():
            return states
        for platform in CrawlPlatform.ALL:
            standard = browser_data / f"{platform}_user_data_dir"
            cdp = browser_data / f"cdp_{platform}_user_data_dir"
            if not standard.is_dir() and not cdp.is_dir():
                continue
            # updated_at 取两个目录里较新的 mtime，纯展示用途
            stamps = []
            for path in (standard, cdp):
                try:
                    if path.is_dir():
                        stamps.append(path.stat().st_mtime)
                except OSError:
                    continue
            states.append(
                {
                    "platform": platform,
                    "platform_label": CrawlPlatform.LABELS.get(platform, platform),
                    "cdp": cdp.is_dir(),
                    "standard": standard.is_dir(),
                    "updated_at": (
                        datetime.fromtimestamp(max(stamps), tz=timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z")
                        if stamps
                        else ""
                    ),
                }
            )
    except OSError:
        logger.debug("扫描登录态目录失败 | %s", browser_data, exc_info=True)
    return states


def _detect_media_enabled(mc_root: Path) -> bool:
    """检测 MC 是否开了媒体下载（ENABLE_GET_MEIDAS）。

    这个开关没有 CLI 参数，只能改 config/base_config.py —— 检测方式就是
    文本匹配那一行赋值。图文二创需要本地图，页面上会提示用户打开它。
    """
    config_file = mc_root / "config" / "base_config.py"
    try:
        text = config_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return re.search(r"^ENABLE_GET_MEIDAS\s*=\s*True\b", text, re.MULTILINE) is not None


def _detect_zhihu_creator_patch(mc_root: Path) -> bool:
    """检测 MC 的 --creator_id 是否已支持知乎。

    原版 cmd_arg/arg.py 的 creator 分支漏了 zhihu（CLI 传入会被静默忽略），
    我们给 MC 仓库打了补丁。检测 `ZHIHU_CREATOR_URL_LIST` 是否出现在 arg.py
    里：出现 = 补丁在；没出现时创建知乎 creator 任务会被拒绝并给指引。
    """
    arg_file = mc_root / "cmd_arg" / "arg.py"
    try:
        text = arg_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "ZHIHU_CREATOR_URL_LIST" in text


# --------------------------------------------------------------------------
# 安装指引
# --------------------------------------------------------------------------

MC_REPO_URL = "https://github.com/NanmiCoder/MediaCrawler"


def install_hints() -> List[dict]:
    """分步安装指引（只是回给前端展示的文本，后端绝不执行）。"""
    mc_root = settings.CRAWL_MC_ROOT
    return [
        {
            "title": "获取 MediaCrawler 仓库",
            "command": f"git clone {MC_REPO_URL} \"{mc_root}\"",
            "note": f"克隆到 {mc_root}（或修改 backend/.env 里的 CRAWL_MC_ROOT 指向已有目录）",
            "url": MC_REPO_URL,
        },
        {
            "title": "安装依赖（需要 Python ≥ 3.11 与 uv）",
            "command": "uv sync",
            "note": f"在 {mc_root} 目录里执行；这会生成 .venv，后端优先用它调用",
            "url": "",
        },
        {
            "title": "打开媒体下载（图文素材需要本地图）",
            "command": "",
            "note": "编辑 MediaCrawler/config/base_config.py，把 ENABLE_GET_MEIDAS 改为 True",
            "url": "",
        },
        {
            "title": "首次使用请在页面上扫码登录",
            "command": "",
            "note": "选「扫码登录」发起任务，会弹出 Chrome 窗口，用平台 App 扫码；登录态会缓存，之后不用再扫",
            "url": "",
        },
        {
            "title": "装好后回到本页",
            "command": "",
            "note": "点「重新检测」即可，不需要重启服务",
            "url": "",
        },
    ]


def probe_environment(*, refresh: bool = False) -> dict:
    """组装环境自检结果，直接给 `/crawl/environment` 用。

    Returns:
        与 `CrawlEnvironmentResponse` 字段一一对应的字典。**新增键时务必
        同步加到 schemas/crawl_job.py 的同名模型上** —— FastAPI 的
        response_model 会把模型里没有的键静默丢掉。
    """
    payload = _cached_payload(force=refresh)
    install: McInstall = payload["install"]

    warnings = list(payload["warnings"])
    if install.installed and not payload["media_enabled"]:
        warnings.append(
            "MediaCrawler 未开启媒体下载（ENABLE_GET_MEIDAS=False）："
            "任务仍会成功，但图片/视频不会落到本地。图文二创建议打开它"
            "（编辑 MC 的 config/base_config.py 后重新检测）。"
        )
    if not payload["node_version"]:
        warnings.append(
            "未检测到 Node.js：抖音、知乎的请求签名需要它（pyexecjs）。"
            "其它平台不受影响。"
        )

    return {
        "installed": install.installed,
        "ready": install.installed,
        "launcher": list(install.launcher),
        "kind": install.kind,
        "mc_root": install.mc_root,
        "python_version": install.python_version,
        "detail": install.detail,
        "node_version": payload["node_version"],
        "node_required_platforms": [CrawlPlatform.DY, CrawlPlatform.ZHIHU],
        "login_states": payload["login_states"],
        "media_enabled": payload["media_enabled"],
        "zhihu_creator_cli_supported": payload["zhihu_creator_cli_supported"],
        "default_output_dir": str(subdir(CRAWL)),
        "install_hints": install_hints(),
        "warnings": warnings,
    }
