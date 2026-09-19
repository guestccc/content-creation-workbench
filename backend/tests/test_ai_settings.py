"""AI 配置的 .env 读写（ai_settings.py）测试。

行为契约与 test_subtitle_settings.py 一致（共用 app/core/env_file.py 的机制）：
字节级保真、段头管理、原子替换。本文件只钉 AI 三键自己的**值语义**：
- base_url / model 必填；
- api_key 留空 = 不动它（页面只改模型不能把 key 抹掉）；
- 读接口里的 key 永远是掩码；
- 环境变量盖过 .env 时如实报告，且不同步那个键。
"""

import os
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import ai_settings
from app.services.ai_settings import (
    read_ai_settings,
    read_env_values,
    shadowed_keys,
    sync_from_env_file,
    write_ai_settings,
)


@pytest.fixture()
def env_path(tmp_path, monkeypatch):
    """把写入目标换到 tmp_path 里的假 .env。"""
    path = tmp_path / ".env"
    monkeypatch.setattr(ai_settings, "_ENV_PATH", path)
    return path


class TestWrite:
    def test_creates_file_with_section(self, env_path):
        write_ai_settings(
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            api_key="sk-realkey123456789",
        )
        text = env_path.read_text(encoding="utf-8")
        assert "AI（一键成品的文案生成）" in text
        assert "AI_BASE_URL=https://api.deepseek.com/v1" in text
        assert "AI_MODEL=deepseek-chat" in text
        assert "AI_API_KEY=sk-realkey123456789" in text

    def test_updates_existing_keys_in_place(self, env_path):
        env_path.write_text(
            "# ---------- AI（一键成品的文案生成） ----------\n"
            "AI_BASE_URL=https://old.example.com/v1\n"
            "AI_MODEL=old-model\n"
            "AI_API_KEY=sk-oldkey000000000\n"
            "# 别动我\n",
            encoding="utf-8",
        )
        write_ai_settings(
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            api_key="sk-newkey111111111",
        )
        text = env_path.read_text(encoding="utf-8")
        assert "AI_BASE_URL=https://api.deepseek.com/v1\n" in text
        assert "https://old.example.com" not in text
        assert "sk-newkey111111111" in text
        assert "sk-oldkey000000000" not in text
        assert "# 别动我" in text  # 其它行原样保留
        assert text.count("AI_BASE_URL=") == 1  # 不追加出第二份

    def test_empty_api_key_leaves_existing_key_untouched(self, env_path):
        """只想改模型时，key 必须还在 —— 页面回填不了原值（只给掩码）。"""
        env_path.write_text("AI_API_KEY=sk-keepme12345678\n", encoding="utf-8")
        write_ai_settings(
            base_url="https://api.deepseek.com/v1", model="deepseek-chat", api_key=""
        )
        text = env_path.read_text(encoding="utf-8")
        assert "AI_API_KEY=sk-keepme12345678" in text

    def test_base_url_and_model_are_required(self, env_path):
        with pytest.raises(ValueError):
            write_ai_settings(base_url="", model="deepseek-chat")
        with pytest.raises(ValueError):
            write_ai_settings(base_url="https://api.deepseek.com/v1", model="")
        assert not env_path.exists()  # 校验失败一个字节都不写

    def test_value_with_newline_is_rejected(self, env_path):
        """换行是一次配置注入（能追加任意键），必须拦住。"""
        with pytest.raises(ValueError):
            write_ai_settings(
                base_url="https://api.deepseek.com/v1\nAI_API_KEY=stolen",
                model="deepseek-chat",
            )
        assert not env_path.exists()

    def test_base_url_trailing_slash_is_stripped(self, env_path):
        write_ai_settings(
            base_url="https://api.deepseek.com/v1/", model="deepseek-chat"
        )
        assert "AI_BASE_URL=https://api.deepseek.com/v1\n" in env_path.read_text(
            encoding="utf-8"
        )

    def test_crlf_preserved(self, env_path):
        env_path.write_bytes("# 头部\r\nAI_MODEL=old\r\n".encode("utf-8"))
        write_ai_settings(base_url="https://api.deepseek.com/v1", model="new-model")
        data = env_path.read_bytes()
        assert "# 头部\r\n".encode("utf-8") in data
        assert b"AI_MODEL=new-model\r\n" in data

    def test_no_temp_file_left(self, env_path):
        write_ai_settings(base_url="https://api.deepseek.com/v1", model="deepseek-chat")
        leftovers = [
            p for p in env_path.parent.iterdir() if p.name.startswith(".env.")
        ]
        assert leftovers == []


class TestRead:
    def test_read_env_values_distinguishes_missing_and_empty(self, env_path):
        env_path.write_text("AI_MODEL=\n", encoding="utf-8")
        values = read_env_values()
        assert values["AI_MODEL"] == ""
        assert values["AI_BASE_URL"] is None
        assert values["AI_API_KEY"] is None

    def test_read_ai_settings_masks_key(self, env_path, monkeypatch):
        monkeypatch.setattr(settings, "AI_API_KEY", "sk-secretkey98765432")
        monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.deepseek.com/v1")
        monkeypatch.setattr(settings, "AI_MODEL", "deepseek-chat")
        payload = read_ai_settings()
        assert payload["api_key_present"] is True
        assert "sk-secretkey98765432" not in payload["api_key_masked"]
        assert payload["api_key_masked"].endswith("5432")
        assert payload["warning"] == ""

    def test_shadowed_keys_reported(self, monkeypatch):
        monkeypatch.setenv("AI_API_KEY", "sk-fromenv12345678")
        try:
            assert "AI_API_KEY" in shadowed_keys()
            payload = read_ai_settings()
            assert "AI_API_KEY" in payload["shadowed_keys"]
            assert "优先级高于 .env" in payload["warning"]
        finally:
            monkeypatch.delenv("AI_API_KEY", raising=False)


class TestSync:
    def test_sync_updates_settings_singleton(self, env_path, monkeypatch):
        monkeypatch.setattr(settings, "AI_BASE_URL", "https://stale.example.com")
        monkeypatch.setattr(settings, "AI_MODEL", "stale-model")
        monkeypatch.setattr(settings, "AI_API_KEY", "")
        env_path.write_text(
            "AI_BASE_URL=https://api.deepseek.com/v1\n"
            "AI_MODEL=deepseek-chat\n"
            "AI_API_KEY=sk-synced123456789\n",
            encoding="utf-8",
        )
        assert sync_from_env_file() is True
        assert settings.AI_BASE_URL == "https://api.deepseek.com/v1"
        assert settings.AI_MODEL == "deepseek-chat"
        assert settings.AI_API_KEY == "sk-synced123456789"

    def test_sync_skips_shadowed_keys(self, env_path, monkeypatch):
        """环境变量占着的键不同步：运行中行为必须和重启后一致。"""
        monkeypatch.setattr(settings, "AI_API_KEY", "sk-env-wins-000001")
        monkeypatch.setenv("AI_API_KEY", "sk-env-wins-000001")
        env_path.write_text("AI_API_KEY=sk-file-loses-00002\n", encoding="utf-8")
        try:
            sync_from_env_file()
            assert settings.AI_API_KEY == "sk-env-wins-000001"
        finally:
            monkeypatch.delenv("AI_API_KEY", raising=False)

    def test_sync_missing_key_falls_back_to_default(self, env_path, monkeypatch):
        """用户把那行删干净了 = 回退默认值，而不是留着旧值。"""
        monkeypatch.setattr(settings, "AI_MODEL", "stale-model")
        env_path.write_text("# 空文件\n", encoding="utf-8")
        assert sync_from_env_file() is True
        assert settings.AI_MODEL == "deepseek-chat"

    def test_sync_noop_when_consistent(self, env_path, monkeypatch):
        monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.deepseek.com/v1")
        monkeypatch.setattr(settings, "AI_MODEL", "deepseek-chat")
        monkeypatch.setattr(settings, "AI_API_KEY", "")
        env_path.write_text("", encoding="utf-8")
        assert sync_from_env_file() is False
