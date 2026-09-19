"""Voicebox 服务的环境自检。

与其它功能的探测（subtitle_env / crawler_env）有一处根本不同：**没有「装在哪」
这个问题**。Voicebox 不是要我们去找的命令行工具，而是一个自己起服务的桌面应用 ——
所以自检就是「按配置的地址连一下，看它说什么」，连不上就是「没开」，不是错误。

与 crawler_env 一致的两条约定：
- **任何失败都不抛异常**：没装/没开是这个功能的正常状态，页面要据此显示指引，
  不该变成 500；
- 探测结果带 TTL 缓存（`VOICEBOX_ENV_CACHE_SECONDS`），写入地址后由调用方
  `reset_cache()`；测试也用它。
"""

import threading
import time
from typing import Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.logging import get_logger
from app.core.materials import DUBBING, subdir
from app.services import voicebox_client, voicebox_settings
from app.services.voicebox_client import VoiceboxError

logger = get_logger(__name__)

_cache_lock = threading.Lock()
_cache: Optional[Tuple[float, dict]] = None

#: 模型下载卡住时的国内解锁方式。Voicebox 从 huggingface.co 拉模型（1.7B 约 3.5GB），
#: 国内直连会超时 —— 实测症状是下到一半彻底不动（分片停在半路、进程还活着）。
#: huggingface_hub 认 HF_ENDPOINT，换成镜像即可；**已下载的分片会续传**，
#: 所以这句里「已经下载的部分会接着下」不是安慰话。
_HF_MIRROR_HINT = (
    "模型下载一直卡住不动，多半是国内连不上 huggingface.co："
    "设置环境变量 HF_ENDPOINT=https://hf-mirror.com 后重启 Voicebox，"
    "已经下载的部分会接着下。"
)


def install_hints() -> List[dict]:
    """连不上时的分步指引。**命令只是文本**，后端绝不替用户下载安装。"""
    return [
        {
            "title": "1. 下载并安装 Voicebox",
            "command": "",
            "note": "免费开源（MIT），在 GitHub Releases 下载 Windows 的 Setup 安装包（约 500MB）。",
            "url": "https://github.com/jamiepine/voicebox/releases",
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
            "note": "在 Voicebox 的「Profiles」里克隆一段自己的声音，或直接用预设音色；"
            "模型会在首次生成时自动下载（1.7B 约 3.5GB）。"
            "国内连不上 huggingface.co：先设 HF_ENDPOINT=https://hf-mirror.com 再下，否则会卡在半路。",
            "url": "",
        },
        {
            "title": "4. 回到这里点「重新检测」",
            "command": "",
            "note": "如果 Voicebox 跑在别的机器上（远程 GPU），用「指定服务地址」改成本机可达的地址。",
            "url": "",
        },
    ]


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
    """真跑一次探测：/health + 音色列表。"""
    base_url = settings.VOICEBOX_BASE_URL
    output_dir = str(subdir(DUBBING))
    warnings: List[str] = []

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
    gpu_available = bool(health.get("gpu_available"))
    ready = reachable and model_downloaded is not False

    fix_hint = ""
    if not reachable:
        fix_hint = "先打开 Voicebox 桌面端；如果它在别的机器上跑，点「指定服务地址」改地址。"
    elif not ready:
        fix_hint = "在 Voicebox 里先随便生成一次，模型会自动下载，之后这里就能用了。"

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
            warnings.append("当前模型还没有下载完，第一次生成会先花时间下载（1.7B 约 3.5GB）。")
            warnings.append(_HF_MIRROR_HINT)
        if profile_count == 0:
            warnings.append(
                "Voicebox 里还没有音色：在它的「Profiles」里建一个（克隆或预设）之后，"
                "这里就能选到了。"
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
        "install_hints": [] if reachable else install_hints(),
        "warnings": warnings,
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
