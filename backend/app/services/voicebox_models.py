"""能用来配音的模型目录：把「选哪个模型」这件事说清楚。

**为什么需要这一层**：上游 `/models/status` 给的是**全部** 18 个模型的下载状态，
里面混着三类东西 —— 能配音的 TTS、给 /transcribe 用的 whisper、给 /llm/generate
用的 qwen3。它也没有「人话名字」和「这个模型好在哪」，只有 `model_name` 和一句
英文 display_name。而 `/generate` 收的又不是 `model_name`，是**两个正交参数**：

    engine      qwen / qwen_custom_voice / luxtts / chatterbox / chatterbox_turbo
                / tada / kokoro
    model_size  1.7B / 0.6B / 1B / 3B —— 且只有部分 engine 分尺寸

也就是说「模型」在上游那边是 (engine, model_size) 这个二元组，而 /models/status
里是扁平的 `model_name`。本模块就是这两者之间的那张对照表，外加一句中文说明 ——
页面上的下拉、生成接口的参数校验，都从这一份目录出发，不再各写各的白名单。

**一处已知的脆弱性**：`model_name` 是上游的版本相关字符串。若将来上游改了名
（比如 `qwen-tts-1.7B` → 别的拼法），对照表就对不上了。这时不会崩，只会退化成
「上游列表里没找到这个模型」→ 页面上标成「状态未知」而不是「未下载」。要修就是
改下面 CATALOG 里的字符串（对照一遍 `GET /models/status` 的实际返回即可）。
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.services import voicebox_client
from app.services.voicebox_client import VoiceboxError


@dataclass(frozen=True)
class DubbingModel:
    """目录里的一项：一个能用来配音的具体模型。

    Attributes:
        engine: 上游 /generate 的 `engine` 参数。
        model_size: 上游 /generate 的 `model_size` 参数；**空串表示这个引擎不分
            尺寸，提交时整个字段都不发**（上游对它是正则校验，空串会被 422 顶回）。
        model_name: 上游 /models/status 里的标识，本模块用它跟下载状态对上号。
        label: 页面上显示的名字（中文，比上游那句英文 display_name 好读）。
        note: 一句话说清它适合什么场景 —— 用户是靠这句话选的，不是靠参数名。
    """

    engine: str
    model_size: str
    model_name: str
    label: str
    note: str


#: 配音可选模型的顺序表。**顺序即页面下拉的顺序**，第一个是兜底默认项。
#:
#: 表里每一项的 model_name 都对着上游 `GET /models/status` 的实际返回值核过
#: （2026-09 在 Voicebox 0.5.0 上逐个比对）。体积只在项目已有依据时才写：
#: 0.6B 的 2.4GB 来自上游返回的 size_mb，1.7B 的 3.5GB 沿用 voicebox_env 里
#: 本来就写给用户的那句提示，不多编数字。
CATALOG: List[DubbingModel] = [
    DubbingModel(
        engine="qwen",
        model_size="1.7B",
        model_name="qwen-tts-1.7B",
        label="Qwen TTS 1.7B",
        note="音质最好、也最慢；约 3.5GB",
    ),
    DubbingModel(
        engine="qwen",
        model_size="0.6B",
        model_name="qwen-tts-0.6B",
        label="Qwen TTS 0.6B",
        note="比 1.7B 快得多，音质一般；约 2.4GB",
    ),
    DubbingModel(
        engine="qwen_custom_voice",
        model_size="1.7B",
        model_name="qwen-custom-voice-1.7B",
        label="Qwen 定制音色 1.7B",
        note="认得音色自带的语气描述（instruct），音质优先",
    ),
    DubbingModel(
        engine="qwen_custom_voice",
        model_size="0.6B",
        model_name="qwen-custom-voice-0.6B",
        label="Qwen 定制音色 0.6B",
        note="同上，更快更省",
    ),
    DubbingModel(
        engine="luxtts",
        model_size="",
        model_name="luxtts",
        label="LuxTTS",
        note="轻量，CPU 上也能跑得比较快",
    ),
    DubbingModel(
        engine="chatterbox",
        model_size="",
        model_name="chatterbox-tts",
        label="Chatterbox",
        note="多语言",
    ),
    DubbingModel(
        engine="chatterbox_turbo",
        model_size="",
        model_name="chatterbox-turbo",
        label="Chatterbox Turbo",
        note="英文，支持情绪标签",
    ),
    DubbingModel(
        engine="tada",
        model_size="1B",
        model_name="tada-1b",
        label="TADA 1B",
        note="英文",
    ),
    DubbingModel(
        engine="tada",
        model_size="3B",
        model_name="tada-3b-ml",
        label="TADA 3B",
        note="多语言，体积最大",
    ),
    DubbingModel(
        engine="kokoro",
        model_size="",
        model_name="kokoro",
        label="Kokoro 82M",
        note="体积最小、最快；英文为主",
    ),
]


def find(engine: str, model_size: str) -> Optional[DubbingModel]:
    """按 (engine, model_size) 找目录项；不在目录里返回 None。

    生成接口拿它做参数校验 —— 用户能选的只有目录里的项，接口就只认这些，
    免得前端能选、后端拒收。`model_size` 要按**原样**比对（空串就是空串）。
    """
    for item in CATALOG:
        if item.engine == engine and item.model_size == model_size:
            return item
    return None


def default_choice() -> DubbingModel:
    """兜底默认模型（目录第一项）。上游状态读不到时，页面的默认值用它。"""
    return CATALOG[0]


def _status_by_name() -> Dict[str, Dict[str, Any]]:
    """拉上游模型状态，按 model_name 索引。

    Raises:
        VoiceboxError: 连不上 / 超时 / 上游报错（由调用方决定怎么呈现）。
    """
    return {
        str(item.get("model_name") or ""): item
        for item in voicebox_client.list_models()
    }


def list_models() -> List[Dict[str, Any]]:
    """目录 + 上游下载状态的合并结果，给页面上的模型下拉用。

    每项在 DubbingModel 的字段之外，还带上 `downloaded` / `downloading` /
    `loaded` / `size_mb`。**`downloaded` 可能是 None**：上游列表里没有这个
    model_name（多半是版本改了名），此时是「不知道」而不是「没下载」——
    页面上要据此区分措辞，不能把未知说成未下载。

    Raises:
        VoiceboxError: 上游读不到（由接口层翻成用户可读的话）。
    """
    status = _status_by_name()
    items: List[Dict[str, Any]] = []
    for model in CATALOG:
        upstream = status.get(model.model_name)
        items.append(
            {
                "engine": model.engine,
                "model_size": model.model_size,
                "model_name": model.model_name,
                "label": model.label,
                "note": model.note,
                "downloaded": (
                    None if upstream is None else bool(upstream.get("downloaded"))
                ),
                "downloading": bool(upstream.get("downloading")) if upstream else False,
                "loaded": bool(upstream.get("loaded")) if upstream else False,
                "size_mb": upstream.get("size_mb") if upstream else None,
            }
        )
    return items


def downloaded_any() -> Optional[bool]:
    """目录里是否**至少有一个**模型已经下好。

    给环境自检用，所以**失败安全：任何异常都退化成 None（不知道），绝不抛**——
    自检的契约是「绝不因为一次探测失败就 500」，而且「模型下没下」本来就只是
    一条提醒，不值得把整个自检拖垮。

    /health 在 0.5.0 上恒返回 `model_downloaded: null`，所以自检实际靠这个函数
    来判断「一个都没下」——那种情况下 `ready` 不该是 True。

    Returns:
        True / False；上游读不到时 None。
    """
    try:
        status = _status_by_name()
    except VoiceboxError:
        return None
    known = [item for item in CATALOG if item.model_name in status]
    if not known:
        # 目录里一项都对不上：多半是上游改了 model_name，这时说「没下载」是错的
        return None
    return any(bool(status[item.model_name].get("downloaded")) for item in known)
