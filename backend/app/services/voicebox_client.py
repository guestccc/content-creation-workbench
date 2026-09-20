"""Voicebox（本地 AI 语音工作室）的 HTTP 客户端。

Voicebox 是一个外部桌面应用，装着 `%APPDATA%/sh.voicebox.app` 那套数据，并在
本机起一个 FastAPI 服务（默认 `http://127.0.0.1:17493`）。它**不是**命令行工具：
**本模块**不起子进程、不解析 stdout、不管它的生命周期，只发 HTTP —— 所以
「装在哪、跑没跑」这类问题的答案只有一个：连得上就用，连不上就如实告诉用户。

（唯一涉及进程的地方是 `services/voicebox_restart.py`：改完下载源必须重启桌面端
才生效。两个模块**互不导入** —— 那边只管进程、不管 HTTP，就绪与否由前端轮询
自检接口判断，不在那边等。）

设计取舍（与 services/ai_client.py 同一套）：
- **同步 httpx.Client**：整个后端是同步路由 + 线程 worker，没有 async 的必要；
- **配置快照成 VoiceboxConfig**：测试可以构造一个只改 base_url 的配置，不必动 settings；
- **错误收敛成 VoiceboxError.kind**：调用方只关心「给用户看什么话、要不要重试」，
  不关心是 ConnectTimeout 还是 ReadTimeout；
- **transport 关键字只给测试注入 httpx.MockTransport**：生产传 None。

上游接口（来自它仓库的 docs/openapi.json，本文件只用到这五个）：
    GET  /health              服务状态 + 模型/GPU 情况
    GET  /models/status       所有模型的下载/加载状态（真相在这里，见下）
    GET  /profiles            音色列表
    POST /generate            生成语音（**同步阻塞**返回结果，长文案要等）
    GET  /audio/{id}          取生成出来的音频字节

**关于 /health 的 model_downloaded**：0.5.0 上它恒为 `null`（实测：模型明明下好了
也报 null），而 `/models/status` 里每条模型都带准确的 downloaded / loaded / size_mb。
所以「模型下没下」一律以 /models/status 为准，/health 那个字段只在非 null 时参考
（见 services/voicebox_env.py 的兜底）。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 自检用的短超时：服务没开时本机是「连接被拒」（毫秒级失败），但地址填成
#: 一台不可达的远程机器时可能一直挂着 —— 自检不该把页面卡住 600 秒。
PROBE_TIMEOUT_SECONDS = 5.0

#: 音色列表这类「小请求」的超时：不值得按生成的长超时等。
QUICK_TIMEOUT_SECONDS = 15.0

#: 音频扩展名白名单：只从上游给的内容类型/路径后缀里认这些，其余一律按 .wav。
AUDIO_EXTENSIONS = (".wav", ".mp3", ".flac", ".m4a", ".ogg")

_CONTENT_TYPE_EXT = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/ogg": ".ogg",
}


@dataclass(frozen=True)
class VoiceboxConfig:
    """一次调用所需的配置（从 settings 快照，便于测试构造）。"""

    base_url: str
    timeout_seconds: int

    @property
    def api(self) -> str:
        """去掉尾部斜杠的基地址（拼路径时统一按 `{api}/xxx`）。"""
        return (self.base_url or "").strip().rstrip("/")


def voicebox_config() -> VoiceboxConfig:
    """从全局 settings 取当前生效的 Voicebox 配置。"""
    return VoiceboxConfig(
        base_url=settings.VOICEBOX_BASE_URL,
        timeout_seconds=settings.VOICEBOX_TIMEOUT_SECONDS,
    )


class VoiceboxError(Exception):
    """Voicebox 调用失败的统一形态：kind 给程序分支用，user_message 给人看。

    kind 取值：
    - unreachable：连不上（没开桌面端 / 地址填错 / 端口不通），引导用户去开；
    - timeout：请求发出去了但超时，长文案在 CPU 上是有可能的，可重试；
    - http：其它非 2xx，4xx 一般是文案/参数问题，重试没意义；
    - not_found：上游说这次生成的音频不存在（可能被它自己清理了）；
    - bad_response：200 但内容不是预期形状，多半是版本不匹配。
    """

    def __init__(self, kind: str, user_message: str, *, detail: str = ""):
        super().__init__(user_message)
        self.kind = kind
        self.user_message = user_message
        self.detail = detail


def _client(
    cfg: VoiceboxConfig,
    timeout: float,
    transport: Optional[httpx.BaseTransport],
) -> httpx.Client:
    """构造一个指向 Voicebox 的客户端。

    `trust_env=False` 是必须的：Voicebox 跑在**本机**（或内网），而装了梯子 /
    公司代理的机器上 Windows 会给进程注入 http_proxy（实测本机就有），
    于是发往 127.0.0.1 的请求被塞进代理，回来的是一句「HTTP 502 / 超时」——
    用户看到的是「服务异常」，而真实原因是「Voicebox 没开」，这种误导比不连还糟。
    代理只对公网服务有意义（那条路走 ai_client），对桌面部服务一律直连。

    要连的若是**需要经代理**才能到达的远程地址，当前不支持 —— 那种部署应该给
    Voicebox 一个直连可达的地址（同内网），而不是让这个代理绕出去。
    """
    return httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(float(timeout)),
        trust_env=False,
    )


def _network_error(cfg: VoiceboxConfig, exc: httpx.HTTPError) -> VoiceboxError:
    """把传输层异常翻译成用户能照做的话。"""
    if isinstance(exc, httpx.TimeoutException):
        return VoiceboxError(
            "timeout",
            "Voicebox 响应超时：长文案在没显卡的机器上会慢很多，可以缩短文案再试",
            detail=str(exc)[:200],
        )
    return VoiceboxError(
        "unreachable",
        f"连不上 Voicebox 服务（{cfg.api}）：请先打开 Voicebox 桌面端，"
        "或在页面上指定服务地址",
        detail=str(exc)[:200],
    )


def _error_snippet(response: httpx.Response) -> str:
    """从错误响应里抠一小段说明文字（限长，防止把整页 HTML 塞进错误消息）。"""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200].strip()
    if isinstance(payload, dict):
        # FastAPI 的 HTTPException 是 {"detail": "..."}；校验错误是 {"detail": [...]}
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()[:200]
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:200]
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"].strip()[:200]
    return response.text[:200].strip()


def _raise_for_status(response: httpx.Response, *, what: str) -> None:
    """非 2xx 一律抛 VoiceboxError（404 单独归一类）。"""
    if response.status_code == 404:
        raise VoiceboxError("not_found", f"Voicebox 说{what}不存在（404）")
    snippet = _error_snippet(response)
    if response.status_code >= 500:
        raise VoiceboxError(
            "http",
            f"Voicebox 服务内部错误（HTTP {response.status_code}）",
            detail=snippet,
        )
    raise VoiceboxError(
        "http",
        f"Voicebox 拒绝了这次请求（HTTP {response.status_code}）：{snippet or what}",
        detail=snippet,
    )


def probe(
    *,
    config: Optional[VoiceboxConfig] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> Dict[str, Any]:
    """探一次服务状态。**连不上不抛异常** —— 那是正常状态，要如实展示。

    Returns:
        `{reachable, status, model_loaded, model_downloaded, model_size,
        gpu_available, vram_used_mb, detail}`；连不上时 `reachable=False`，
        其余字段给安全默认值。
    """
    cfg = config or voicebox_config()
    try:
        with _client(cfg, PROBE_TIMEOUT_SECONDS, transport) as client:
            response = client.get(f"{cfg.api}/health")
    except httpx.HTTPError as exc:
        error = _network_error(cfg, exc)
        return {"reachable": False, "detail": error.user_message}
    if response.status_code != 200:
        return {
            "reachable": False,
            "detail": f"Voicebox 服务返回 HTTP {response.status_code}："
            f"{_error_snippet(response) or '接口不像是 Voicebox 的'}",
        }
    try:
        payload = response.json()
    except ValueError:
        return {"reachable": False, "detail": "Voicebox 的 /health 返回了非 JSON 内容"}
    if not isinstance(payload, dict):
        return {"reachable": False, "detail": "Voicebox 的 /health 返回了意外的结构"}

    return {
        "reachable": True,
        "status": str(payload.get("status") or ""),
        "model_loaded": bool(payload.get("model_loaded")),
        "model_downloaded": payload.get("model_downloaded"),
        "model_size": payload.get("model_size"),
        "gpu_available": bool(payload.get("gpu_available")),
        "vram_used_mb": payload.get("vram_used_mb"),
        "detail": "服务正常",
    }


def list_profiles(
    *,
    config: Optional[VoiceboxConfig] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> List[Dict[str, Any]]:
    """拉音色列表。

    Raises:
        VoiceboxError: 连不上 / 超时 / 上游报错。
    """
    cfg = config or voicebox_config()
    try:
        with _client(cfg, QUICK_TIMEOUT_SECONDS, transport) as client:
            response = client.get(f"{cfg.api}/profiles")
    except httpx.HTTPError as exc:
        raise _network_error(cfg, exc) from exc
    if response.status_code != 200:
        _raise_for_status(response, what="音色列表")
    try:
        payload = response.json()
    except ValueError as exc:
        raise VoiceboxError(
            "bad_response", "Voicebox 返回的音色列表不是 JSON", detail=str(exc)[:200]
        ) from exc
    if not isinstance(payload, list):
        raise VoiceboxError(
            "bad_response", "Voicebox 返回的音色列表结构不对（预期是数组）"
        )
    return [item for item in payload if isinstance(item, dict)]


def list_models(
    *,
    config: Optional[VoiceboxConfig] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> List[Dict[str, Any]]:
    """拉模型清单（**全部**模型，不只是能配音的）。

    上游这一份混着三类：TTS（能配音）、whisper（转写）、qwen3（LLM）。本函数原样
    返回，由调用方按自己的用途筛 —— 筛选规则属于业务知识，不该塞进这个纯 HTTP 层
    （见 services/voicebox_models.py）。

    Raises:
        VoiceboxError: 连不上 / 超时 / 上游报错。
    """
    cfg = config or voicebox_config()
    try:
        with _client(cfg, QUICK_TIMEOUT_SECONDS, transport) as client:
            response = client.get(f"{cfg.api}/models/status")
    except httpx.HTTPError as exc:
        raise _network_error(cfg, exc) from exc
    if response.status_code != 200:
        _raise_for_status(response, what="模型列表")
    try:
        payload = response.json()
    except ValueError as exc:
        raise VoiceboxError(
            "bad_response", "Voicebox 返回的模型列表不是 JSON", detail=str(exc)[:200]
        ) from exc
    # 形状是 {"models": [...]}；个别版本可能直接给数组，两种都认
    if isinstance(payload, dict):
        payload = payload.get("models")
    if not isinstance(payload, list):
        raise VoiceboxError("bad_response", "Voicebox 返回的模型列表结构不对")
    return [item for item in payload if isinstance(item, dict)]


def generate(
    text: str,
    *,
    profile_id: str,
    language: str = "zh",
    engine: str = "qwen",
    model_size: str = "1.7B",
    config: Optional[VoiceboxConfig] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> Dict[str, Any]:
    """调一次 /generate（同步阻塞，返回 `{id, audio_path, duration, ...}`）。

    这是唯一一个用长超时的接口：上游是同步生成，长文案要跑很久。

    `engine` 与 `model_size` 是**两个正交参数**：engine 选哪套 TTS 实现
    （qwen / luxtts / kokoro / chatterbox / tada …），model_size 只在部分是
    有意义的（qwen 分 1.7B/0.6B，tada 分 1B/3B，而 luxtts / kokoro / chatterbox
    只有一个尺寸）。所以 **model_size 为空串时整个字段都不发** —— 上游对它的
    校验是 `^(1\.7B|0\.6B|1B|3B)$`，发个空串会被 422 顶回来。

    Raises:
        VoiceboxError: 连不上 / 超时 / 上游报错 / 返回结构不对。
    """
    cfg = config or voicebox_config()
    payload: Dict[str, Any] = {
        "profile_id": profile_id,
        "text": text,
        "language": language,
        "engine": engine,
    }
    if model_size:
        payload["model_size"] = model_size
    try:
        with _client(cfg, cfg.timeout_seconds, transport) as client:
            response = client.post(f"{cfg.api}/generate", json=payload)
    except httpx.HTTPError as exc:
        raise _network_error(cfg, exc) from exc
    if response.status_code != 200:
        _raise_for_status(response, what="这次生成")
    try:
        result = response.json()
    except ValueError as exc:
        raise VoiceboxError(
            "bad_response", "Voicebox 的生成结果不是 JSON", detail=str(exc)[:200]
        ) from exc
    if not isinstance(result, dict) or not result.get("id"):
        raise VoiceboxError(
            "bad_response",
            "Voicebox 的生成结果里没有音频 id（版本不匹配？）",
            detail=str(result)[:200],
        )
    return result


def _extension_from(content_type: str, hint: str) -> str:
    """按 Content-Type 推断扩展名，推断不出来再退回上游给的路径后缀。"""
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime in _CONTENT_TYPE_EXT:
        return _CONTENT_TYPE_EXT[mime]
    suffix = Path(hint or "").suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return suffix
    return ".wav"


def fetch_audio(
    generation_id: str,
    *,
    hint: str = "",
    config: Optional[VoiceboxConfig] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> Tuple[bytes, str]:
    """取生成出来的音频字节，返回 `(bytes, 扩展名)`。

    为什么不直接读上游返回的 `audio_path`：那是 **Voicebox 那台机器上**的路径，
    远程模式下根本没有意义；走 `/audio/{id}` 拿字节才是两种部署都成立的取法。

    Args:
        generation_id: 上游生成结果的 id。
        hint: 上游 `audio_path`（可空），只在 Content-Type 认不出来时兜底推扩展名。
    """
    cfg = config or voicebox_config()
    try:
        with _client(cfg, QUICK_TIMEOUT_SECONDS, transport) as client:
            response = client.get(f"{cfg.api}/audio/{generation_id}")
    except httpx.HTTPError as exc:
        raise _network_error(cfg, exc) from exc
    if response.status_code != 200:
        _raise_for_status(response, what="这次生成的音频")
    if not response.content:
        raise VoiceboxError("bad_response", "Voicebox 返回了空音频")
    return response.content, _extension_from(response.headers.get("content-type", ""), hint)
