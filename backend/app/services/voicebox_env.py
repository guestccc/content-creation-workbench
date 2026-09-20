"""Voicebox 服务的环境自检。

与其它功能的探测（subtitle_env / crawler_env）有一处根本不同：**没有「装在哪」
这个问题**。Voicebox 不是要我们去找的命令行工具，而是一个自己起服务的桌面应用 ——
所以自检就是「按配置的地址连一下，看它说什么」，连不上就是「没开」，不是错误。

**唯一新增的例外**：页面上要出现「设置镜像」「重启 Voicebox」两个动作，而这两个
动作能不能用取决于一些「因为不知道装在哪所以本来不该问」的信息（系统是什么、
下载源设没设、装在哪）。这些探测的实现全在 `voicebox_mirror` / `voicebox_restart`
里，本模块只做**汇总**，并且每一个都包在 `_safe_*` 里 —— 探测失败退化成
「不支持 + 一句 warnings」，绝不让自检变成 500（这是下面第二条约定不可让步的部分）。

与 crawler_env 一致的两条约定：
- **任何失败都不抛异常**：没装/没开是这个功能的正常状态，页面要据此显示指引，
  不该变成 500；
- 探测结果带 TTL 缓存（`VOICEBOX_ENV_CACHE_SECONDS`），写入地址后由调用方
  `reset_cache()`；测试也用它。
"""

import threading
import time
from typing import List, Optional, Tuple

from app.core.config import settings
from app.core.logging import get_logger
from app.core.materials import DUBBING, subdir
from app.core.platform import platform_key, platform_label
from app.services import (
    voicebox_client,
    voicebox_mirror,
    voicebox_models,
    voicebox_restart,
    voicebox_settings,
)
from app.services.voicebox_client import VoiceboxError

logger = get_logger(__name__)

_cache_lock = threading.Lock()
_cache: Optional[Tuple[float, dict]] = None

#: Voicebox 的发布页。三个平台的产物品名不同，链接是同一个。
RELEASES_URL = "https://github.com/jamiepine/voicebox/releases"

#: 本模块「能自动重启桌面端」的系统。Linux 上桌面应用怎么拿到环境变量、怎么被
#: 拉起都取决于发行版，我们没有可靠做法，就如实说不支持（与 user_env 一致）。
_RESTARTABLE_PLATFORMS = ("macos", "windows")


# --------------------------------------------------------------------------
# 分平台文案
# --------------------------------------------------------------------------


def _install_step_1_note() -> str:
    """第 1 步（下载安装）的说明 —— 三个平台拿到的产物不一样。"""
    key = platform_key()
    if key == "windows":
        return (
            "免费开源（MIT）。在 GitHub Releases 下载 Windows 安装包 "
            "voicebox_x64-setup.exe（约 500MB），双击安装。"
        )
    if key == "macos":
        return (
            "免费开源（MIT）。在 GitHub Releases 下载 .dmg：Apple Silicon（M 系列芯片）"
            "选 aarch64 版，Intel 机器选 x64 版，拖进「应用程序」。"
        )
    return (
        "官方目前只发 macOS 和 Windows 版，Linux 需要自己从源码构建；"
        "也可以把 Voicebox 跑在另一台机器上，回本页用「指定服务地址」连过去。"
    )


def _install_step_3_note(*, mirror_actionable: bool) -> str:
    """第 3 步（建音色 + 模型下载）的说明。

    把「下载会卡住」这件坑提前说在这里，别等用户真的下到一半才发现 ——
    国内直连 huggingface.co 是本功能最高频的失败点。
    """
    head = (
        "在 Voicebox 的「Profiles」里克隆一段自己的声音，或直接用预设音色；"
        "模型会在首次生成时自动下载（1.7B 约 3.5GB）。"
    )
    if mirror_actionable:
        tail = (
            "国内直连 huggingface.co 会卡在半路 —— 装好打开 Voicebox 之后回到本页，"
            f"点「设置镜像」（把下载源换成 {voicebox_mirror.HF_MIRROR_URL}），"
            "再点「重启 Voicebox」。"
        )
    else:
        tail = (
            f"国内直连 huggingface.co 会卡在半路 —— 先设 "
            f"{voicebox_mirror.HF_MIRROR_KEY}={voicebox_mirror.HF_MIRROR_URL} "
            "再启动 Voicebox。"
        )
    return head + tail


def install_hints(*, mirror_actionable: bool = False) -> List[dict]:
    """连不上时的分步指引。**命令只是文本**，后端绝不替用户下载安装。

    Args:
        mirror_actionable: 当前环境能不能由本页设置下载源。为 True 时第 3 步
            引导用户点页面上的按钮，否则给出要手动设的环境变量。
    """
    return [
        {
            "title": "1. 下载并安装 Voicebox",
            "command": "",
            "note": _install_step_1_note(),
            "url": RELEASES_URL,
        },
        {
            "title": "2. 打开 Voicebox 桌面端",
            "command": "",
            "note": "它启动时会自动在后台起服务（默认 127.0.0.1:17493），"
            "左下角状态指示灯变绿就绪。",
            "url": "",
        },
        {
            "title": "3. 建一个音色",
            "command": "",
            "note": _install_step_3_note(mirror_actionable=mirror_actionable),
            "url": "",
        },
        {
            "title": "4. 回到这里点「重新检测」",
            "command": "",
            "note": "如果 Voicebox 跑在别的机器上（远程 GPU），用「指定服务地址」"
            "改成本机可达的地址；中途重启过 Voicebox，也点一下「重新检测」。",
            "url": "",
        },
    ]


def _hf_mirror_hint(
    mirror: voicebox_mirror.MirrorStatus, *, actionable: bool
) -> str:
    """模型没下完时的镜像提示。

    三种处境给三句不同的话，因为**用户下一步该做什么不一样**：
    能由本页设置的 → 引导点按钮；已经设好的 → 只差重启；设不了的 → 给手工命令。

    措辞注意：一律写「本页的『设置镜像』」而不是「下面的按钮」—— 按钮在第一个
    提示框里、这段话在它下面，位置关系会随布局变，指方位迟早指错。
    """
    url = voicebox_mirror.HF_MIRROR_URL
    key = voicebox_mirror.HF_MIRROR_KEY
    if not actionable:
        return (
            "模型下载一直卡住不动，多半是国内连不上 huggingface.co："
            f"请在启动 Voicebox **之前**设置环境变量 {key}={url}，再重启它。"
            "已经下载的部分会接着下。"
        )
    if mirror.is_recommended:
        return (
            f"下载源镜像已经设好了（{key}={url}），但它要**重启 Voicebox 之后**才生效："
            "点本页的「重启 Voicebox」。已经下载的部分会接着下。"
        )
    return (
        "模型下载一直卡住不动，多半是国内连不上 huggingface.co："
        f"点本页的「设置镜像」把下载源换成 {url}，再点「重启 Voicebox」；"
        "已经下载的部分会接着下。"
    )


# --------------------------------------------------------------------------
# 汇总「页面按钮能不能用」所需的探测（都失败安全）
# --------------------------------------------------------------------------


def _safe_mirror_status() -> voicebox_mirror.MirrorStatus:
    """读下载源状态。

    不需要 try/except：`voicebox_mirror.status()` 的契约就是「任何失败都退化成
    『读不到』，绝不抛异常」—— 在那里保证一次，好过每个调用方各兜一层。
    """
    return voicebox_mirror.status()


def _safe_app_path(warnings: List[str]) -> str:
    """找 Voicebox 装在哪（只为判断「重启」按钮能不能用）。

    这里必须兜异常：`find_app()` 会 `stat` 若干路径、跑一次 Spotlight 查询，
    而自检的契约是「绝不因为探测失败而 500」。
    """
    try:
        app = voicebox_restart.find_app()
    except Exception as exc:  # noqa: BLE001 - 探测失败的原因不重要，退化成「找不到」即可
        logger.warning("查找 Voicebox 安装位置失败：%s", exc)
        warnings.append("查找 Voicebox 安装位置时出错，页面上的「重启 Voicebox」可能用不了。")
        return ""
    return str(app) if app else ""


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------


def _cached_payload(*, refresh: bool) -> dict:
    """带 TTL 缓存的探测：页面每次进都要自检，不该每次都真连一次上游。"""
    global _cache

    ttl = settings.VOICEBOX_ENV_CACHE_SECONDS
    now = time.monotonic()
    if not refresh and _cache is not None:
        cached_at, cached = _cache
        if now - cached_at < ttl:
            return cached

    payload = _probe_uncached()
    with _cache_lock:
        _cache = (now, payload)
    return payload


def _probe_uncached() -> dict:
    """真跑一次探测：/health + 音色列表 + 页面按钮所需的本地状态。"""
    base_url = settings.VOICEBOX_BASE_URL
    output_dir = str(subdir(DUBBING))
    warnings: List[str] = []

    # 地址指向远程机器时，「设环境变量」「重启桌面端」都不该在本机做 —— 页面据此
    # 藏掉按钮、并如实说「去那台机器上设置」，而不是给一个点了没反应的按钮。
    is_local = voicebox_settings.is_local_base_url()
    mirror = _safe_mirror_status()
    mirror_actionable = mirror.supported and is_local
    # 只在本地部署时才去找安装位置：远程部署下本机的 Voicebox 装没装与页面无关
    app_path = _safe_app_path(warnings) if is_local else ""

    platform = platform_key()
    restart_supported = (
        is_local and platform in _RESTARTABLE_PLATFORMS and bool(app_path)
    )

    health = voicebox_client.probe()
    reachable = bool(health.get("reachable"))
    detail = str(health.get("detail") or "")

    profile_count = 0
    if reachable:
        try:
            profile_count = len(voicebox_client.list_profiles())
        except VoiceboxError as exc:
            # 服务在但音色拉不到：不推翻「服务可用」的结论，只提醒一句
            warnings.append(f"音色列表读取失败：{exc.user_message}")

    model_downloaded = health.get("model_downloaded")
    if reachable and model_downloaded is None:
        # /health 在 Voicebox 0.5.0 上这个字段**恒为 null**（实测：模型明明下好了
        # 也报 null），不回退的话「一个模型都没下」的机器上 ready 照样是 True ——
        # 用户点生成，只会拿到一句看不懂的上游报错。真相在 /models/status 里，
        # 那里每条模型都带准确的 downloaded（见 voicebox_client 顶部注释）。
        # 返回值可能是 None（上游读不到）：那就不猜，维持「不知道」的语义。
        model_downloaded = voicebox_models.downloaded_any()
    gpu_available = bool(health.get("gpu_available"))
    ready = reachable and model_downloaded is not False

    fix_hint = ""
    if not reachable:
        fix_hint = "先打开 Voicebox 桌面端；如果它在别的机器上跑，点「指定服务地址」改地址。"
    elif not ready:
        fix_hint = (
            "在 Voicebox 里先随便生成一次，模型会自动下载，之后这里就能用了。"
            + ("下载一直卡住就点本页的「设置镜像」，再点「重启 Voicebox」。"
               if mirror_actionable else "")
        )

    if reachable:
        if not gpu_available:
            # 措辞要小心：**AMD/Intel 机器上这个字段一样是 False** —— 它说的是
            # 「没有能用的 CUDA 加速」，不是「没有显卡」。Voicebox 只带 CUDA 后端
            # （Windows 上 AMD 既没有 ROCm 也没有 DirectML），对着一张独显说
            # 「没检测到 GPU」会让人以为驱动有问题，白折腾。
            warnings.append(
                "没有检测到 Voicebox 能用的 GPU 加速：它只支持 NVIDIA 的 CUDA，"
                "AMD/Intel 卡都会退回 CPU 跑。生成会明显慢一些，换 0.6B 模型能快很多。"
            )
        if model_downloaded is False:
            warnings.append(
                "还没有下载任何配音模型，第一次生成会先花很长时间下载（1.7B 约 3.5GB）。"
            )
            # 下面两条都以 mirror_actionable 为门槛 —— 它**就是**前端显示那个按钮的
            # 条件（hf_mirror_supported），文案里指名一个按钮，就得跟按钮同生共死。
            # 门槛漏掉时踩过的坑：服务地址指向远程机器、而本机又设过 HF_ENDPOINT 时，
            # 页面藏了按钮、这里却还在说「可以点设置镜像」，用户去找一个不存在的东西。
            # （远程下本机这个变量本来就影响不到那台机器，不说也罢。）
            if mirror_actionable and voicebox_mirror.is_customized(mirror.value):
                # 用户自己设过别的源：提醒一句就好，**不覆盖** —— 可能是他的内网镜像
                warnings.append(
                    f"当前 {voicebox_mirror.HF_MIRROR_KEY} 指向 "
                    f"{voicebox_mirror.host_of(mirror.value)}，不是本页推荐的镜像。"
                    "这是你自己设的就不用管；下载卡住时可以点「设置镜像」换成推荐的。"
                )
            warnings.append(_hf_mirror_hint(mirror, actionable=mirror_actionable))
            if mirror_actionable and mirror.value and not mirror.persistent:
                warnings.append(
                    f"{voicebox_mirror.HF_MIRROR_KEY} 设了但**没有持久化**："
                    "注销或重启电脑后会失效。点本页的「设置镜像」可以让它长期生效。"
                )
            if not is_local:
                warnings.append(
                    f"当前服务地址是远程的（{base_url}）：下载源要在**跑 Voicebox 的那台"
                    "机器上**设置，本页的设置与重启都作用不到它。"
                )
        if profile_count == 0:
            warnings.append(
                "Voicebox 里还没有音色：在它的「Profiles」里建一个（克隆或预设）之后，"
                "这里就能选到了。"
            )

    # 服务连得上、却没找到安装位置 —— 这才是值得提醒的处境（「重启」按钮会失效）。
    # 服务都连不上时这一条不追加：那种情况 install_hints 已经把话说完了，多一条
    # 「找不到安装位置」只会让第一屏更吵，而且用户下一步动作本来就是先装它。
    if reachable and is_local and platform in _RESTARTABLE_PLATFORMS and not app_path:
        warnings.append(
            "没有找到 Voicebox 的安装位置，页面上的「重启 Voicebox」会用不了"
            "（已查找：" + "、".join(voicebox_restart.searched_paths()) + "）。"
            "请手动退出 Voicebox 再打开，然后点「重新检测」。"
        )

    return {
        "ready": ready,
        "base_url": base_url,
        "base_url_source": voicebox_settings.base_url_source(),
        "reachable": reachable,
        "status": str(health.get("status") or ""),
        "model_loaded": bool(health.get("model_loaded")),
        "model_downloaded": model_downloaded,
        "model_size": health.get("model_size"),
        "gpu_available": gpu_available,
        "vram_used_mb": health.get("vram_used_mb"),
        "profile_count": profile_count,
        "default_output_dir": output_dir,
        "detail": detail,
        "fix_hint": fix_hint,
        "install_hints": [] if reachable else install_hints(mirror_actionable=mirror_actionable),
        "warnings": warnings,
        # ---- 页面按钮的可用性（前端**不做**平台判断，一律按这些字段渲染）----
        "platform": platform,
        "platform_label": platform_label(),
        "hf_mirror_supported": mirror_actionable,
        "hf_mirror_value": mirror.value or "",
        "hf_mirror_is_recommended": mirror.is_recommended,
        "hf_mirror_persistent": mirror.persistent,
        "voicebox_app_path": app_path,
        "restart_supported": restart_supported,
    }


def probe_environment(*, refresh: bool = False) -> dict:
    """环境自检（字段与 schemas/voicebox.VoiceboxEnvironmentResponse 对齐）。

    注意：新增字段必须同时加到那个响应模型上，否则 response_model 会把它悄悄丢掉。
    """
    return _cached_payload(refresh=refresh)


def reset_cache() -> None:
    """清空缓存（改完服务地址、或测试里用）。"""
    global _cache
    with _cache_lock:
        _cache = None
