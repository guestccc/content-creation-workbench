"""口播语速的 .env 读写（finalcut_settings.py）测试。

行为契约与 test_ai_settings.py 一致（共用 app/core/env_file.py 的机制），
本文件只钉语速自己的**值语义**：

- 范围校验（1.0–15.0）在写值函数里，越界抛 ValueError（API 层翻 400）；
- 读路径永远不抛：`.env` 被手改坏了就退回默认值，不能让环境接口 500；
- 热同步必须落成 float —— `float * str` 的 TypeError 会等到任务跑起来才炸；
- **段头不能撞车**：本模块的段头标记绝不能命中 AI 那段，否则语速键会被插进
  AI 配置里（env_file 的段头判定是子串匹配）。

⚠️ `_ENV_PATH` 的替换是 **autouse** 的、不靠每个用例自己声明参数：漏一次就是
真的读写开发机上的 `backend/.env`（里面是用户自己的 API key）。
"""

from pathlib import Path

import pytest

from app.core.config import default_chars_per_second, settings
from app.services import finalcut_settings
from app.services.finalcut_settings import (
    MAX_CHARS_PER_SECOND,
    MIN_CHARS_PER_SECOND,
    normalize_chars_per_second,
    read_chars_per_second,
    read_finalcut_settings,
    sync_from_env_file,
    write_chars_per_second,
)


@pytest.fixture(autouse=True)
def env_path(tmp_path, monkeypatch) -> Path:
    """把读写目标换到 tmp_path 里的假 .env，并把 settings 单例的值恢复原样。"""
    path = tmp_path / ".env"
    monkeypatch.setattr(finalcut_settings, "_ENV_PATH", path)
    monkeypatch.setattr(
        settings, "FINALCUT_CHARS_PER_SECOND", default_chars_per_second()
    )
    return path


class TestNormalize:
    def test_accepts_bounds(self):
        assert normalize_chars_per_second(MIN_CHARS_PER_SECOND) == MIN_CHARS_PER_SECOND
        assert normalize_chars_per_second(MAX_CHARS_PER_SECOND) == MAX_CHARS_PER_SECOND

    def test_rounds_to_two_decimals(self):
        """87 字念了 15 秒 → 5.8 是用户手填的值，别被填成 5.8666666。"""
        assert normalize_chars_per_second(87 / 15) == 5.8

    @pytest.mark.parametrize("value", [0.9, 15.1, 0, -3, "很快", None])
    def test_rejects_out_of_range_and_non_numeric(self, value):
        with pytest.raises(ValueError):
            normalize_chars_per_second(value)


class TestWrite:
    def test_creates_file_with_own_section(self, env_path):
        write_chars_per_second(5.8)
        text = env_path.read_text(encoding="utf-8")
        assert "# ---------- 一键成品（口播语速） ----------" in text
        assert "FINALCUT_CHARS_PER_SECOND=5.8" in text

    def test_updates_existing_key_in_place(self, env_path):
        env_path.write_text(
            "FINALCUT_CHARS_PER_SECOND=4.5\n# 别动我\n", encoding="utf-8"
        )
        write_chars_per_second(5.8)
        text = env_path.read_text(encoding="utf-8")
        assert "FINALCUT_CHARS_PER_SECOND=5.8" in text
        assert "FINALCUT_CHARS_PER_SECOND=4.5" not in text
        assert "# 别动我" in text
        assert text.count("FINALCUT_CHARS_PER_SECOND=") == 1  # 不追加出第二份

    def test_does_not_land_in_ai_section(self, env_path):
        """段头标记不能命中 AI 那段 —— 命中了语速键就插进 AI 配置里了。

        env_file._is_section_header 是子串匹配，AI 段头
        `# ---------- AI（一键成品的文案生成） ----------` 里带「一键成品」，
        本模块的标记若也带这四个字就会被误判。
        """
        env_path.write_text(
            "# ---------- AI（一键成品的文案生成） ----------\n"
            "AI_MODEL=deepseek-chat\n"
            "\n"
            "# 其它段\n"
            "OTHER_KEY=1\n",
            encoding="utf-8",
        )
        write_chars_per_second(5.8)
        lines = env_path.read_text(encoding="utf-8").splitlines()
        ai_index = next(i for i, line in enumerate(lines) if "一键成品的文案生成" in line)
        # AI 段头下面紧挨着的仍是它自己的键，没被塞进语速
        assert lines[ai_index + 1] == "AI_MODEL=deepseek-chat"
        assert "AI_MODEL=deepseek-chat" in lines
        assert "OTHER_KEY=1" in lines
        rate_index = next(
            i for i, line in enumerate(lines) if line.startswith("FINALCUT_CHARS_PER_SECOND=")
        )
        assert rate_index > ai_index

    def test_section_mark_does_not_appear_in_ai_header(self):
        """上一条用例的根因，直接钉在常量上：两个段头不能互相命中。"""
        assert finalcut_settings._SECTION_MARK not in "AI（一键成品的文案生成）"
        assert "一键成品的文案生成" not in finalcut_settings._SECTION_HEADER

    @pytest.mark.parametrize("value", [0.5, 20])
    def test_out_of_range_writes_nothing(self, env_path, value):
        with pytest.raises(ValueError):
            write_chars_per_second(value)
        assert not env_path.exists()  # 校验失败一个字节都不写

    def test_no_temp_file_left(self, env_path):
        write_chars_per_second(5.8)
        leftovers = [p for p in env_path.parent.iterdir() if p.name.startswith(".env.")]
        assert leftovers == []


class TestRead:
    def test_missing_key_returns_none(self, env_path):
        env_path.write_text("# 空文件\n", encoding="utf-8")
        assert read_chars_per_second() is None

    def test_blank_value_returns_none(self, env_path):
        env_path.write_text("FINALCUT_CHARS_PER_SECOND=\n", encoding="utf-8")
        assert read_chars_per_second() is None

    def test_broken_value_returns_none_instead_of_raising(self, env_path):
        """用户手改坏了 .env 不该让整个环境接口 500。"""
        env_path.write_text("FINALCUT_CHARS_PER_SECOND=很快\n", encoding="utf-8")
        assert read_chars_per_second() is None

    def test_read_finalcut_settings_reports_effective_value(self, env_path, monkeypatch):
        monkeypatch.setattr(settings, "FINALCUT_CHARS_PER_SECOND", 5.8)
        payload = read_finalcut_settings()
        assert payload["chars_per_second"] == 5.8
        assert payload["chars_per_second_default"] == default_chars_per_second()
        assert payload["chars_per_second_warning"] == ""

    def test_read_finalcut_settings_reports_shadowing(self, monkeypatch):
        monkeypatch.setenv("FINALCUT_CHARS_PER_SECOND", "9.9")
        try:
            payload = read_finalcut_settings()
            assert "优先级高于 .env" in payload["chars_per_second_warning"]
        finally:
            monkeypatch.delenv("FINALCUT_CHARS_PER_SECOND", raising=False)


class TestSync:
    def test_sync_sets_float_not_str(self, env_path, monkeypatch):
        """read_value 给的是字符串，直接 setattr 会让 `时长 × 语速` 在任务里炸。"""
        monkeypatch.setattr(settings, "FINALCUT_CHARS_PER_SECOND", 4.5)
        env_path.write_text("FINALCUT_CHARS_PER_SECOND=5.8\n", encoding="utf-8")
        assert sync_from_env_file() is True
        assert isinstance(settings.FINALCUT_CHARS_PER_SECOND, float)
        assert settings.FINALCUT_CHARS_PER_SECOND == 5.8

    def test_sync_missing_key_falls_back_to_default(self, env_path, monkeypatch):
        """用户把那行删干净了 = 回退默认值，而不是留着旧值。"""
        monkeypatch.setattr(settings, "FINALCUT_CHARS_PER_SECOND", 9.9)
        env_path.write_text("# 空文件\n", encoding="utf-8")
        assert sync_from_env_file() is True
        assert settings.FINALCUT_CHARS_PER_SECOND == default_chars_per_second()

    def test_sync_broken_value_falls_back_to_default(self, env_path, monkeypatch):
        monkeypatch.setattr(settings, "FINALCUT_CHARS_PER_SECOND", 9.9)
        env_path.write_text("FINALCUT_CHARS_PER_SECOND=abc\n", encoding="utf-8")
        assert sync_from_env_file() is True
        assert settings.FINALCUT_CHARS_PER_SECOND == default_chars_per_second()

    def test_sync_skips_shadowed_env_var(self, env_path, monkeypatch):
        """环境变量占着这个键时不同步：运行中行为必须和重启后一致。"""
        monkeypatch.setattr(settings, "FINALCUT_CHARS_PER_SECOND", 5.0)
        monkeypatch.setenv("FINALCUT_CHARS_PER_SECOND", "9.9")
        env_path.write_text("FINALCUT_CHARS_PER_SECOND=5.8\n", encoding="utf-8")
        try:
            assert sync_from_env_file() is False
            assert settings.FINALCUT_CHARS_PER_SECOND == 5.0
        finally:
            monkeypatch.delenv("FINALCUT_CHARS_PER_SECOND", raising=False)

    def test_sync_noop_when_consistent(self, env_path):
        env_path.write_text("", encoding="utf-8")
        assert sync_from_env_file() is False
