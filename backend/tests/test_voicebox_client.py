"""Voicebox HTTP 客户端（voicebox_client.py）的测试。

全程用 httpx.MockTransport，不发真实网络请求。钉住的行为：
- 请求形状（/generate 的字段、audio 的取法）；
- probe() 的「连不上不是异常」约定（返回 reachable=False 而不是抛）；
- 错误分类（unreachable/timeout/http/not_found/bad_response）；
- 扩展名从 Content-Type 推、推不出退回 audio_path 后缀、再退 .wav。
"""

import json

import httpx
import pytest

from app.services.voicebox_client import (
    VoiceboxConfig,
    VoiceboxError,
    fetch_audio,
    generate,
    list_models,
    list_profiles,
    probe,
)


def _config(**overrides) -> VoiceboxConfig:
    base = dict(base_url="http://127.0.0.1:17493", timeout_seconds=600)
    base.update(overrides)
    return VoiceboxConfig(**base)


HEALTH_OK = {
    "status": "ready",
    "model_loaded": True,
    "model_downloaded": True,
    "model_size": "1.7B",
    "gpu_available": True,
    "vram_used_mb": 512.0,
}


class TestProbe:
    def test_reachable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/health"
            return httpx.Response(200, json=HEALTH_OK)

        result = probe(config=_config(), transport=httpx.MockTransport(handler))
        assert result["reachable"] is True
        assert result["model_downloaded"] is True
        assert result["gpu_available"] is True
        assert result["model_size"] == "1.7B"

    def test_unreachable_returns_not_raises(self):
        """连不上是正常状态（桌面端没开），必须返回 reachable=False。"""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        result = probe(config=_config(), transport=httpx.MockTransport(handler))
        assert result["reachable"] is False
        assert "连不上" in result["detail"]

    def test_non_200_is_unreachable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        result = probe(config=_config(), transport=httpx.MockTransport(handler))
        assert result["reachable"] is False
        assert "500" in result["detail"]

    def test_non_json_is_unreachable(self):
        """探测到一个不是 Voicebox 的服务时如实报告，不抛。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>nginx</html>")

        result = probe(config=_config(), transport=httpx.MockTransport(handler))
        assert result["reachable"] is False


class TestListProfiles:
    def test_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/profiles"
            return httpx.Response(200, json=[{"id": "p1", "name": "我的声音"}])

        items = list_profiles(config=_config(), transport=httpx.MockTransport(handler))
        assert items == [{"id": "p1", "name": "我的声音"}]

    def test_unreachable_raises_voicebox_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(VoiceboxError) as excinfo:
            list_profiles(config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "unreachable"
        assert "连不上" in excinfo.value.user_message

    def test_non_list_payload_is_bad_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": []})

        with pytest.raises(VoiceboxError) as excinfo:
            list_profiles(config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "bad_response"


class TestListModels:
    def test_unwraps_the_models_key(self):
        """上游给的是 `{"models": [...]}`，本函数把它拆到列表就返回。"""
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/models/status"
            return httpx.Response(
                200,
                json={"models": [{"model_name": "qwen-tts-0.6B", "downloaded": True}]},
            )

        items = list_models(config=_config(), transport=httpx.MockTransport(handler))

        assert items == [{"model_name": "qwen-tts-0.6B", "downloaded": True}]

    def test_bare_list_payload_is_also_accepted(self):
        """个别版本可能直接给数组 —— 两种形状都认，免得升级即挂。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"model_name": "kokoro", "downloaded": False}])

        items = list_models(config=_config(), transport=httpx.MockTransport(handler))

        assert items == [{"model_name": "kokoro", "downloaded": False}]

    def test_unreachable_raises_voicebox_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(VoiceboxError) as excinfo:
            list_models(config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "unreachable"

    def test_unexpected_payload_is_bad_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"models": "不是数组"})

        with pytest.raises(VoiceboxError) as excinfo:
            list_models(config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "bad_response"


class TestGenerate:
    def test_request_shape_and_result(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={"id": "gen_1", "duration": 3.2, "audio_path": "/x/gen_1.wav"},
            )

        result = generate(
            "大家好，今天推荐一款保温杯",
            profile_id="p1",
            language="zh",
            model_size="1.7B",
            config=_config(),
            transport=httpx.MockTransport(handler),
        )
        assert result["id"] == "gen_1"

        body = json.loads(seen[0].content)
        assert seen[0].url.path == "/generate"
        assert body == {
            "profile_id": "p1",
            "text": "大家好，今天推荐一款保温杯",
            "language": "zh",
            "engine": "qwen",
            "model_size": "1.7B",
        }

    def test_engine_selects_the_model(self):
        """engine 别丢了：丢了就静默退回上游默认的 qwen，用户选的 Kokoro 白选。"""
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"id": "gen_1", "duration": 1.0})

        generate(
            "hi",
            profile_id="p1",
            engine="kokoro",
            model_size="",
            config=_config(),
            transport=httpx.MockTransport(handler),
        )

        body = json.loads(seen[0].content)
        assert body["engine"] == "kokoro"

    def test_empty_model_size_is_omitted(self):
        """不分尺寸的引擎（kokoro / luxtts / chatterbox）**整个字段都不发**。

        上游对 model_size 的校验是 `^(1\\.7B|0\\.6B|1B|3B)$` —— 发个空串过去会被
        422 顶回来，用户看到的是「Voicebox 拒绝了这次请求」，而真正的原因在我们
        这边多发了一个字段。
        """
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"id": "gen_1", "duration": 1.0})

        generate(
            "hi",
            profile_id="p1",
            engine="kokoro",
            model_size="",
            config=_config(),
            transport=httpx.MockTransport(handler),
        )

        assert "model_size" not in json.loads(seen[0].content)

    def test_timeout_is_timeout_kind(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(VoiceboxError) as excinfo:
            generate("文案", profile_id="p1", config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "timeout"
        assert "超时" in excinfo.value.user_message

    def test_4xx_translates_upstream_detail(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"detail": "text is too long"})

        with pytest.raises(VoiceboxError) as excinfo:
            generate("文案", profile_id="p1", config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "http"
        assert "text is too long" in excinfo.value.user_message

    def test_5xx_is_http_kind(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="cuda out of memory")

        with pytest.raises(VoiceboxError) as excinfo:
            generate("文案", profile_id="p1", config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "http"
        assert "内部错误" in excinfo.value.user_message

    def test_result_without_id_is_bad_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        with pytest.raises(VoiceboxError) as excinfo:
            generate("文案", profile_id="p1", config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "bad_response"


class TestFetchAudio:
    def test_bytes_and_extension_from_content_type(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/audio/gen_1"
            return httpx.Response(200, content=b"RIFF....", headers={"content-type": "audio/wav"})

        content, ext = fetch_audio("gen_1", config=_config(), transport=httpx.MockTransport(handler))
        assert content == b"RIFF...."
        assert ext == ".wav"

    def test_extension_falls_back_to_hint(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"data", headers={"content-type": "application/octet-stream"}
            )

        _content, ext = fetch_audio(
            "gen_1", hint="/tmp/out.mp3", config=_config(), transport=httpx.MockTransport(handler)
        )
        assert ext == ".mp3"

    def test_extension_defaults_to_wav(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"data")

        _content, ext = fetch_audio("gen_1", config=_config(), transport=httpx.MockTransport(handler))
        assert ext == ".wav"

    def test_404_is_not_found(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "generation not found"})

        with pytest.raises(VoiceboxError) as excinfo:
            fetch_audio("gen_x", config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "not_found"

    def test_empty_content_is_bad_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"")

        with pytest.raises(VoiceboxError) as excinfo:
            fetch_audio("gen_1", config=_config(), transport=httpx.MockTransport(handler))
        assert excinfo.value.kind == "bad_response"
