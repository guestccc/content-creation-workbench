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
    stream_chat,
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


# ----------------------------------------------------------------------
# 流式（stream_chat）
# ----------------------------------------------------------------------


def _chunk(**delta) -> str:
    """一帧 `data:` 行，delta 里放什么就发什么（reasoning_content / content）。"""
    payload = {"choices": [{"delta": delta}]}
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _usage_chunk(tokens: int = 30) -> str:
    """include_usage 的收尾帧：choices 为空、只有 usage（DeepSeek 的真实形状）。"""
    return "data: " + json.dumps({"choices": [], "usage": {"total_tokens": tokens}}) + "\n\n"


DONE_FRAME = "data: [DONE]\n\n"


def _stream_response(frames) -> httpx.Response:
    """200 的 SSE 响应：body 是**逐帧的字节迭代器**。

    必须用迭代器而不是拼好的 bytes —— 一次性给完的话，httpx 会把整个 body
    当已读内容，`iter_lines()` 就没有「边到边」可言了，测试也就测不到
    「已经吐过增量就不再重试」这类行为。
    """
    return httpx.Response(200, content=iter([f.encode("utf-8") for f in frames]))


def _stream_then_timeout(request: httpx.Request, frames) -> object:
    """先吐完 frames，再在读取中抛 ReadTimeout —— 模拟「思维链吐到一半断流」。"""

    def _body():
        for frame in frames:
            yield frame.encode("utf-8")
        raise httpx.ReadTimeout("断流", request=request)

    return _body()


class TestStreamChat:
    def test_deltas_carry_reasoning_and_content_separately(self):
        """思维链与正文分两路 yield，末尾一帧只带 usage。"""
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _stream_response(
                [
                    _chunk(reasoning_content="先想"),
                    _chunk(reasoning_content="再写"),
                    _chunk(content="答案"),
                    _chunk(content="：好"),
                    _usage_chunk(77),
                    DONE_FRAME,
                ]
            )

        deltas = list(
            stream_chat(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        )
        assert [d.reasoning for d in deltas] == ["先想", "再写", "", "", ""]
        assert [d.content for d in deltas] == ["", "", "答案", "：好", ""]
        assert deltas[-1].usage["total_tokens"] == 77
        # 思维链是给用户看的，正文才是落库内容，两边不能串
        assert "答案" not in "".join(d.reasoning for d in deltas)

        body = json.loads(seen[0].content)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert body["response_format"] == {"type": "json_object"}
        # 思考模式参数必须带上（DeepSeek 思考模式默认开、effort 默认 high，
        # 显式写出来是为了不被供应商那边悄悄改浅）
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "high"

    def test_thinking_disabled_drops_both_private_params(self):
        """关掉思考模式：一个私有键都不发（别家 OpenAI 兼容端点会被 400 挡回来）。"""
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _stream_response([_chunk(content="好"), DONE_FRAME])

        list(
            stream_chat(
                MESSAGES,
                config=_config(thinking=False),
                transport=httpx.MockTransport(handler),
            )
        )
        body = json.loads(seen[0].content)
        assert "thinking" not in body
        assert "reasoning_effort" not in body
        # 其余参数照旧，只是「退回老行为」，不是换一套请求
        assert body["stream"] is True
        assert body["response_format"] == {"type": "json_object"}

    def test_noise_lines_are_skipped(self):
        """注释行 / event 行 / 坏 JSON / 非对象 JSON 一律跳过，不毁掉整轮。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return _stream_response(
                [
                    ": keep-alive\n\n",
                    "event: message\n",
                    _chunk(reasoning_content="真·思维链"),
                    "data: {不是 JSON\n\n",
                    "data: [1, 2, 3]\n\n",
                    _chunk(content="真·正文"),
                    DONE_FRAME,
                ]
            )

        deltas = list(
            stream_chat(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        )
        assert [d.reasoning for d in deltas] == ["真·思维链", ""]
        assert [d.content for d in deltas] == ["", "真·正文"]

    def test_empty_delta_is_not_yielded(self):
        """心跳帧（delta 全空）不往上冒泡 —— 否则调用方要为它做无意义的 yield。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return _stream_response(
                [_chunk(), _chunk(reasoning_content=""), _chunk(content="有内容"), DONE_FRAME]
            )

        deltas = list(
            stream_chat(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        )
        assert len(deltas) == 1
        assert deltas[0].content == "有内容"

    def test_no_retry_after_first_delta(self):
        """已经吐过增量：连接再断也不重试（重试 = 前端看到思维链重来一遍）。"""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200, content=_stream_then_timeout(request, [_chunk(reasoning_content="半截")])
            )

        stream = stream_chat(
            MESSAGES, config=_config(), transport=httpx.MockTransport(handler)
        )
        assert next(stream).reasoning == "半截"
        with pytest.raises(AiError) as excinfo:
            next(stream)
        assert excinfo.value.kind == "timeout"
        assert len(calls) == 1

    def test_timeout_before_first_delta_is_retried(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, content=_stream_then_timeout(request, []))
            return _stream_response([_chunk(content="第二次成功"), DONE_FRAME])

        deltas = list(
            stream_chat(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        )
        assert calls and len(calls) == 2
        assert deltas[0].content == "第二次成功"

    def test_rate_limit_before_first_delta_is_retried(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, json={"error": {"message": "too many requests"}})
            return _stream_response([_chunk(content="好"), DONE_FRAME])

        deltas = list(
            stream_chat(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        )
        assert len(calls) == 2
        assert deltas[0].content == "好"

    def test_auth_is_fatal_and_not_retried(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})

        with pytest.raises(AiError) as excinfo:
            list(stream_chat(MESSAGES, config=_config(), transport=httpx.MockTransport(handler)))
        assert excinfo.value.kind == "auth"
        assert len(calls) == 1

    def test_server_error_body_is_readable(self):
        """非 200 的响应体要先 read() 才能看：漏了会变成 ResponseNotRead ——
        错误消息里就只剩「HTTP 500」，排查时看不到服务端说了什么。"""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(500, json={"error": {"message": "server exploded"}})

        with pytest.raises(AiError) as excinfo:
            list(stream_chat(MESSAGES, config=_config(), transport=httpx.MockTransport(handler)))
        assert excinfo.value.kind == "http"
        assert "server exploded" in excinfo.value.detail
        assert len(calls) == 2  # 5xx 可重试

    def test_stream_without_any_delta_is_bad_response(self):
        """200 但一个字都没有（[DONE] 直接收尾）：当坏响应，且不重试。"""
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _stream_response([DONE_FRAME])

        with pytest.raises(AiError) as excinfo:
            list(stream_chat(MESSAGES, config=_config(), transport=httpx.MockTransport(handler)))
        assert excinfo.value.kind == "bad_response"
        assert len(calls) == 2

    def test_stream_ending_without_done_marker_is_fine(self):
        """少了 [DONE]（有些供应商不发）不影响结果：迭代自然结束。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return _stream_response([_chunk(content="好")])

        deltas = list(
            stream_chat(MESSAGES, config=_config(), transport=httpx.MockTransport(handler))
        )
        assert [(d.reasoning, d.content) for d in deltas] == [("", "好")]

    def test_missing_config_fails_before_any_request(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("未配置时不该发请求")

        with pytest.raises(AiError) as excinfo:
            list(
                stream_chat(
                    MESSAGES,
                    config=_config(api_key=""),
                    transport=httpx.MockTransport(handler),
                )
            )
        assert excinfo.value.kind == "config"


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
