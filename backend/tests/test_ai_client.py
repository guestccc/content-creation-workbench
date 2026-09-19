"""AI 客户端（ai_client.py）的测试。

全程用 httpx.MockTransport，不发真实网络请求。钉住的行为：
- 请求形状（json_object 响应格式、鉴权头、模型与参数）；
- 错误分类（config/auth/rate_limit/timeout/network/http/bad_response）；
- 重试只发生在「重试可能变好」的失败上（429/5xx/超时/断网/坏响应体）；
- API key 的任何对外形态都不含完整 key。
"""

import json

import httpx
import pytest

from app.services.ai_client import (
    AiConfig,
    AiError,
    chat_json,
    extract_json_object,
    mask_key,
)


def _config(**overrides) -> AiConfig:
    base = dict(
        base_url="https://api.deepseek.com/v1",
        api_key="sk-testkey1234567890",
        model="deepseek-chat",
        temperature=0.9,
        timeout_seconds=5,
        max_tokens=4096,
        max_retries=1,
    )
    base.update(overrides)
    return AiConfig(**base)


def _ok_response(content: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(content, ensure_ascii=False),
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        },
    )


MESSAGES = [{"role": "user", "content": "生成文案，输出 JSON"}]


class TestChatJson:
    def test_success_returns_content_and_usage(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _ok_response({"copies": []})

        result = chat_json(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        assert json.loads(result.content) == {"copies": []}
        assert result.usage["total_tokens"] == 30

        request = seen[0]
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer sk-testkey1234567890"
        body = json.loads(request.content)
        assert body["model"] == "deepseek-chat"
        assert body["temperature"] == 0.9
        assert body["max_tokens"] == 4096
        # DeepSeek 要求开了 json_object 时提示词里必须出现 JSON 字样，
        # 消息构造方（finalcut_copy）负责这件事；客户端负责把参数带上
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"] == MESSAGES

    def test_base_url_trailing_slash_is_normalized(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _ok_response({})

        chat_json(
            MESSAGES,
            config=_config(base_url="https://api.deepseek.com/v1/"),
            transport=httpx.MockTransport(handler),
        )
        assert str(seen[0].url) == "https://api.deepseek.com/v1/chat/completions"

    def test_unconfigured_raises_config_error_without_http(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("未配置时不该发出任何 HTTP 请求")

        with pytest.raises(AiError) as excinfo:
            chat_json(
                MESSAGES,
                config=_config(api_key=""),
                transport=httpx.MockTransport(handler),
            )
        assert excinfo.value.kind == "config"
        assert "AI 配置" in excinfo.value.user_message

    def test_401_is_auth_error_and_not_retried(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})

        with pytest.raises(AiError) as excinfo:
            chat_json(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "auth"
        assert "API key" in excinfo.value.user_message
        assert len(calls) == 1  # key 错了重试一百次也是错

    def test_429_is_retried_then_succeeds(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, json={"error": {"message": "rate limited"}})
            return _ok_response({"ok": True})

        result = chat_json(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        assert json.loads(result.content) == {"ok": True}
        assert len(calls) == 2

    def test_429_exhausting_retries_raises_rate_limit(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})

        with pytest.raises(AiError) as excinfo:
            chat_json(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "rate_limit"

    def test_500_is_retried_then_raises_http(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(500, text="internal server error")

        with pytest.raises(AiError) as excinfo:
            chat_json(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "http"
        assert len(calls) == 2  # 1 次原始 + 1 次重试

    def test_400_is_not_retried(self):
        """4xx（模型名写错之类）重试不会变好，一次就抛。"""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(400, json={"error": {"message": "Model Not Exist"}})

        with pytest.raises(AiError) as excinfo:
            chat_json(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "http"
        assert "400" in excinfo.value.user_message
        assert "Model Not Exist" in excinfo.value.user_message
        assert len(calls) == 1

    def test_timeout_is_retried_then_raises_timeout(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            raise httpx.ReadTimeout("boom", request=request)

        with pytest.raises(AiError) as excinfo:
            chat_json(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "timeout"
        assert len(calls) == 2

    def test_connect_error_is_network(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns failed", request=request)

        with pytest.raises(AiError) as excinfo:
            chat_json(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "network"

    def test_200_with_garbage_body_is_bad_response_and_retried(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, text="<html>not json</html>")

        with pytest.raises(AiError) as excinfo:
            chat_json(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "bad_response"
        assert len(calls) == 2

    def test_200_without_choices_is_bad_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unexpected": True})

        with pytest.raises(AiError) as excinfo:
            chat_json(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "bad_response"


class TestExtractJsonObject:
    def test_plain_json(self):
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
        assert extract_json_object('```\n{"a": 1}\n```') == {"a": 1}

    def test_json_embedded_in_prose(self):
        text = '好的，以下是结果：\n{"a": 1, "b": {"c": 2}}\n希望对你有帮助。'
        assert extract_json_object(text) == {"a": 1, "b": {"c": 2}}

    def test_non_json_raises_bad_response(self):
        with pytest.raises(AiError) as excinfo:
            extract_json_object("我完全不知道怎么回答")
        assert excinfo.value.kind == "bad_response"

    def test_empty_raises_bad_response(self):
        with pytest.raises(AiError):
            extract_json_object("")

    def test_json_array_is_rejected(self):
        """文案产物的顶层必须是对象（analysis + copies），数组不算数。"""
        with pytest.raises(AiError):
            extract_json_object('[{"a": 1}]')


class TestMaskKey:
    def test_normal_key_keeps_head_and_tail(self):
        masked = mask_key("sk-abcdef1234567890")
        assert masked.startswith("sk-")
        assert masked.endswith("7890")
        assert "abcdef123456" not in masked

    def test_short_key_is_fully_masked(self):
        assert mask_key("sk-short") == "****"

    def test_empty_key(self):
        assert mask_key("") == ""
        assert mask_key(None) == ""
