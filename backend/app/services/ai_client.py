"""OpenAI 兼容的 chat/completions 客户端（一键成品的 AI 文案生成走这里）。

默认对接 DeepSeek（`AI_BASE_URL=https://api.deepseek.com/v1`），换任何
OpenAI 兼容供应商只改配置，不改代码。

设计取舍：
- **用 httpx 直连，不引入 openai SDK**：我们只用一个端点（chat/completions）
  加一个参数（response_format=json_object），SDK 的模型抽象用不上，
  而它把超时/重试/错误类型包了一层，排查时反而要多剥一层。
- **错误分类收敛成 AiError.kind**：调用方（文案 runner）只关心「要不要重试、
  给用户看什么话」，不关心是 httpx.ConnectTimeout 还是 ReadTimeout。
- **任何接口都不回传完整 API key**：对外展示一律走 mask_key。
"""

import json
import re
from dataclasses import dataclass
from typing import Dict, List, NamedTuple, Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class AiConfig:
    """一次 AI 调用所需的全部配置（从 settings 快照，便于测试构造）。"""

    base_url: str
    api_key: str
    model: str
    temperature: float
    timeout_seconds: int
    max_tokens: int
    max_retries: int

    @property
    def configured(self) -> bool:
        """三个必填项都非空才算「已配置」。"""
        return bool(
            self.base_url.strip() and self.api_key.strip() and self.model.strip()
        )

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def ai_config() -> AiConfig:
    """从全局 settings 取当前生效的 AI 配置。"""
    return AiConfig(
        base_url=settings.AI_BASE_URL,
        api_key=settings.AI_API_KEY,
        model=settings.AI_MODEL,
        temperature=settings.AI_TEMPERATURE,
        timeout_seconds=settings.AI_TIMEOUT_SECONDS,
        max_tokens=settings.AI_MAX_TOKENS,
        max_retries=settings.AI_MAX_RETRIES,
    )


class AiError(Exception):
    """AI 调用失败的统一形态：kind 给程序分支用，user_message 给人看。

    kind 取值：
    - config：没配置（缺 key 等），重试没意义，引导用户去填；
    - auth：401/403，key 错或没权限，重试没意义；
    - rate_limit：429，可重试；
    - timeout / network：传输层问题，可重试；
    - http：其它非 2xx，不重试（4xx 重试也不会变好）；
    - bad_response：200 但内容不是预期的 JSON，重试一次可能有救。
    """

    def __init__(self, kind: str, user_message: str, *, detail: str = ""):
        super().__init__(user_message)
        self.kind = kind
        self.user_message = user_message
        self.detail = detail


class ChatResult(NamedTuple):
    """一次成功的对话补全。content 是模型原文，usage 供记账/展示。"""

    content: str
    usage: Dict


def mask_key(key: str) -> str:
    """API key 的展示形态：只留头尾，中间打码（`sk-****abcd`）。

    任何接口/日志都不许出现完整 key —— 它写在 `.env` 里，而 `.env` 以外的
    地方（响应体、日志、错误消息）都不该成为泄露面。
    """
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:3]}****{key[-4:]}"


_FENCE_RE = re.compile(r"^```(?:json)?\s*(?P<body>.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def extract_json_object(text: str) -> dict:
    """从模型输出里取出一个 JSON 对象。

    模型即使被要求「只输出 JSON」，也可能裹 ```json 围栏或前后带解说
    文字。依次尝试：整体解析 → 剥围栏 → 取首个 `{` 到最后一个 `}`。
    都失败说明这轮输出不可用，抛 bad_response 让上层决定重试或报错。
    """
    candidate = (text or "").strip()
    if not candidate:
        raise AiError("bad_response", "AI 返回了空内容")

    fence = _FENCE_RE.match(candidate)
    if fence is not None:
        candidate = fence.group("body").strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        # 整体能解析但顶层不是对象（比如模型直接给了一个数组）：
        # 文案产物的契约是 {analysis, copies}，数组不是「围栏/解说」问题，
        # 不能靠截取花括号救回来 —— 救回来形状也不对，直接判 bad_response。
        if isinstance(parsed, dict):
            return parsed
        raise AiError(
            "bad_response",
            "AI 返回的 JSON 顶层不是对象（契约是 {analysis, copies}）",
            detail=candidate[:200],
        )

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(candidate[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise AiError(
        "bad_response",
        "AI 返回的内容不是可用的 JSON（原文已存进任务的 raw_response，可在任务详情里查看）",
        detail=candidate[:200],
    )


def _error_snippet(response: httpx.Response) -> str:
    """从错误响应里抠一小段说明文字（限长，防止把整页 HTML 塞进错误消息）。"""
    try:
        payload = response.json()
        message = payload.get("error", {}).get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:200]
    except ValueError:
        pass
    return response.text[:200].strip()


def chat_json(
    messages: List[Dict[str, str]],
    *,
    config: Optional[AiConfig] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> ChatResult:
    """调一次 chat/completions 并要求 JSON 输出，返回 (content, usage)。

    Args:
        messages: OpenAI 格式的消息列表（system/user/...）。
        config: 不传则用 settings 里的当前配置。
        transport: 测试注入 httpx.MockTransport；生产传 None。

    Raises:
        AiError: 任何失败都归一成它，kind 见类 docstring。
    """
    cfg = config or ai_config()
    if not cfg.configured:
        raise AiError(
            "config",
            "尚未配置 AI：请在页面右上角「AI 配置」里填写 API key（默认对接 DeepSeek）",
        )

    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        # 要求模型输出合法 JSON；提示词里也必须出现 "JSON" 字样，否则
        # DeepSeek 会拒这个参数（它家文档的明确要求）。
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {cfg.api_key}"}

    last_error: Optional[AiError] = None
    attempts = 1 + max(0, cfg.max_retries)
    with httpx.Client(
        transport=transport, timeout=httpx.Timeout(float(cfg.timeout_seconds))
    ) as client:
        for attempt in range(1, attempts + 1):
            try:
                response = client.post(cfg.chat_url, json=payload, headers=headers)
            except httpx.TimeoutException:
                last_error = AiError(
                    "timeout",
                    f"AI 请求超时（超过 {cfg.timeout_seconds} 秒）。"
                    "网络慢或模型繁忙时会这样，可直接重试",
                )
            except httpx.HTTPError as exc:
                last_error = AiError(
                    "network",
                    "连不上 AI 接口：检查本机网络，或确认 AI_BASE_URL 填写正确",
                    detail=str(exc)[:200],
                )
            else:
                status = response.status_code
                if status in (401, 403):
                    raise AiError(
                        "auth",
                        "AI 接口拒绝了 API key（401/403）："
                        "请在「AI 配置」里检查 AI_API_KEY 是否填对、账户是否有余额",
                        detail=_error_snippet(response),
                    )
                if status == 429:
                    last_error = AiError(
                        "rate_limit",
                        "AI 接口限流（429）：稍等片刻再试，或换个时间段",
                        detail=_error_snippet(response),
                    )
                elif status >= 500:
                    last_error = AiError(
                        "http",
                        f"AI 接口服务端错误（HTTP {status}），可稍后重试",
                        detail=_error_snippet(response),
                    )
                elif status != 200:
                    # 其它 4xx：请求本身有问题（模型名错、参数不支持），重试没意义
                    raise AiError(
                        "http",
                        f"AI 接口返回 HTTP {status}：{_error_snippet(response) or '请检查 AI_MODEL 等配置'}",
                    )
                else:
                    result = _parse_response(response)
                    if result is not None:
                        if attempt > 1:
                            logger.info("AI 请求在第 %d 次尝试后成功", attempt)
                        return result
                    last_error = AiError(
                        "bad_response",
                        "AI 接口返回了 200，但响应体不是预期的格式",
                        detail=response.text[:200],
                    )

            if attempt < attempts:
                logger.info(
                    "AI 请求失败（%s），%d/%d 次，重试一次",
                    last_error.kind if last_error else "?",
                    attempt,
                    attempts,
                )

    assert last_error is not None
    raise last_error


def _parse_response(response: httpx.Response) -> Optional[ChatResult]:
    """拆 chat/completions 的响应体；形状不对返回 None（调用方归成 bad_response）。"""
    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(content, str) or not content.strip():
        return None
    usage = payload.get("usage")
    return ChatResult(
        content=content,
        usage=usage if isinstance(usage, dict) else {},
    )
