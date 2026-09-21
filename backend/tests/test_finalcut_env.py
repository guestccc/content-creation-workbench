"""一键成品环境自检（finalcut_env.py）的测试。

三项探测全部打桩：AI 配置用 monkeypatch 改 settings 字段，ffmpeg 用假的
find_tool / verify_tool / run_probe，字体用 tmp_path 里的假文件。钉住的契约：
- AI 没配 key → ready=False 且 fix_hint 能指路；
- ffmpeg 缺 drawtext → 报的是「这个构建没有 drawtext」而不是「没有 ffmpeg」；
- boxborderw 是实测出来的能力标记，不是猜的；
- FINALCUT_FONT_FILE 指向不存在的文件时**不静默回落**到系统字体（写错的
  显式配置必须被看见）。
"""

from pathlib import Path

import pytest

from app.core.config import settings
from app.services import finalcut_env
from app.services.finalcut_env import (
    detect_font,
    probe_ai,
    probe_drawtext,
    probe_environment,
)
from app.services.media_tools import ProbedOutput


@pytest.fixture(autouse=True)
def _clean_ai_settings(monkeypatch):
    """每个用例都从「未配置 AI」开始，要用配置好的形态就自己改。"""
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "AI_MODEL", "deepseek-chat")
    monkeypatch.setattr(settings, "FINALCUT_FONT_FILE", "")


# --------------------------------------------------------------------------
# AI 配置
# --------------------------------------------------------------------------


class TestProbeAi:
    def test_unconfigured(self):
        result = probe_ai()
        assert result["ok"] is False
        assert result["configured"] is False
        assert result["key_present"] is False
        assert result["fix_hint"]  # 必须能指路，不能只说「不行」

    def test_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "AI_API_KEY", "sk-realkey123456789")
        result = probe_ai()
        assert result["ok"] is True
        assert result["base_url"] == "https://api.deepseek.com/v1"
        assert result["model"] == "deepseek-chat"
        # key 只有掩码形态，完整值不出现在这个接口里
        assert result["key_masked"]
        assert "sk-realkey123456789" not in str(result)


# --------------------------------------------------------------------------
# 字体探测
# --------------------------------------------------------------------------


class TestDetectFont:
    def test_override_file_wins(self, tmp_path):
        font = tmp_path / "我的字体.ttf"
        font.write_bytes(b"fake-font")
        settings.FINALCUT_FONT_FILE = str(font)
        choice = detect_font()
        assert choice is not None
        assert choice.file == str(font)
        assert "自定义" in choice.family

    def test_missing_override_does_not_fall_back(self, tmp_path, monkeypatch):
        """显式指定的文件不存在 → None（报出来），不静默回落到候选表。"""
        settings.FINALCUT_FONT_FILE = str(tmp_path / "不存在.ttf")
        # 候选表造一个「本来能命中」的字体，验证确实没有回落
        fallback = tmp_path / "fallback.ttf"
        fallback.write_bytes(b"fake-font")
        monkeypatch.setattr(finalcut_env, "_FONT_CANDIDATES", ((str(fallback), "候选"),))
        assert detect_font() is None

    def test_candidates_first_existing_wins(self, tmp_path, monkeypatch):
        first = tmp_path / "a.ttf"
        second = tmp_path / "b.ttf"
        second.write_bytes(b"fake-font")
        monkeypatch.setattr(
            finalcut_env,
            "_FONT_CANDIDATES",
            ((str(first), "第一"), (str(second), "第二")),
        )
        choice = detect_font()
        assert choice == (str(second), "第二")

    def test_no_font_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            finalcut_env, "_FONT_CANDIDATES", ((str(tmp_path / "x.ttf"), "不存在"),)
        )
        assert detect_font() is None


# --------------------------------------------------------------------------
# ffmpeg drawtext 能力
# --------------------------------------------------------------------------

_FILTERS_WITH_DRAWTEXT = (
    "Filters:\n"
    " T.C drawtext          V->V       Draw text on top of video frames using libfreetype library.\n"
    " ... scale             V->V       Scale the input video size.\n"
)
_FILTERS_WITHOUT_DRAWTEXT = (
    "Filters:\n"
    " ... scale             V->V       Scale the input video size.\n"
)


def _stub_ffmpeg(monkeypatch, *, filters_text, help_text):
    """把 find_tool / verify_tool / run_probe 全部换成假的。

    三个名字都打在 finalcut_env 命名空间上（它 import 的是名字本身，
    不是 media_tools 模块）—— 与 mix_runner 测试的既有教训一致。
    """
    monkeypatch.setattr(finalcut_env, "find_tool", lambda name: f"/fake/{name}")
    monkeypatch.setattr(finalcut_env, "verify_tool", lambda name, path: "7.1")

    def fake_run_probe(argv, **kwargs):
        if "-filters" in argv:
            return ProbedOutput(returncode=0, stdout=filters_text, stderr="")
        if "filter=drawtext" in argv:
            return ProbedOutput(returncode=0, stdout=help_text, stderr="")
        return ProbedOutput(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(finalcut_env, "run_probe", fake_run_probe)


class TestProbeDrawtext:
    def test_ffmpeg_missing(self, monkeypatch):
        monkeypatch.setattr(finalcut_env, "find_tool", lambda name: None)
        result = probe_drawtext()
        assert result["ok"] is False
        assert "ffmpeg" in result["fix_hint"]

    def test_impostor_rejected(self, monkeypatch):
        monkeypatch.setattr(finalcut_env, "find_tool", lambda name: "/fake/ffmpeg")
        monkeypatch.setattr(finalcut_env, "verify_tool", lambda name, path: "")
        result = probe_drawtext()
        assert result["ok"] is False
        assert "自证" in result["detail"] or "不是真的 ffmpeg" in result["detail"]

    def test_drawtext_missing_from_build(self, monkeypatch):
        _stub_ffmpeg(monkeypatch, filters_text=_FILTERS_WITHOUT_DRAWTEXT, help_text="")
        result = probe_drawtext()
        assert result["ok"] is False
        assert result["has_drawtext"] is False
        # 报的是「构建里缺滤镜」，不是「没有 ffmpeg」—— 两者修法完全不同
        assert "drawtext" in result["detail"]
        assert "libfreetype" in result["fix_hint"]

    def test_fix_hint_points_at_a_build_that_actually_has_drawtext(self, monkeypatch):
        """修复建议必须指向真带 drawtext 的构建。

        这条是防回归：早先写的是「macOS 用 brew install ffmpeg（默认带）」，
        但 brew 的普通 ffmpeg formula 不含 libfreetype（实测 9.0.2 的编译
        配置里连 --enable-libfreetype 都没有），带 drawtext 的是另一个
        keg-only 的 ffmpeg-full。照着原话装完，drawtext 照样缺。
        """
        _stub_ffmpeg(monkeypatch, filters_text=_FILTERS_WITHOUT_DRAWTEXT, help_text="")
        hint = probe_drawtext()["fix_hint"]
        assert "ffmpeg-full" in hint
        # keg-only 不 link 就进不了 PATH，不提这句用户装完还是找不到
        assert "keg-only" in hint
        assert "brew install ffmpeg（默认带）" not in hint

    def test_ffmpeg_missing_hint_also_points_at_full_build(self, monkeypatch):
        """「没装 ffmpeg」那条也直接指向带 drawtext 的构建。

        烧字功能上，装一个不带 drawtext 的 ffmpeg 等于没装 —— 不如一次说清，
        省得用户装完普通版再撞一次同样的墙。
        """
        monkeypatch.setattr(finalcut_env, "find_tool", lambda name: None)
        hint = probe_drawtext()["fix_hint"]
        assert "PATH" in hint
        assert "ffmpeg-full" in hint

    def test_full_capability(self, monkeypatch):
        _stub_ffmpeg(
            monkeypatch,
            filters_text=_FILTERS_WITH_DRAWTEXT,
            help_text="drawtext AVOptions:\n  boxborderw        <int>  ...\n",
        )
        result = probe_drawtext()
        assert result["ok"] is True
        assert result["version"] == "7.1"
        assert result["has_drawtext"] is True
        assert result["supports_boxborderw"] is True

    def test_old_ffmpeg_without_boxborderw(self, monkeypatch):
        _stub_ffmpeg(
            monkeypatch,
            filters_text=_FILTERS_WITH_DRAWTEXT,
            help_text="drawtext AVOptions:\n  fontsize          <int>  ...\n",
        )
        result = probe_drawtext()
        assert result["ok"] is True  # 缺 boxborderw 不是不可用
        assert result["supports_boxborderw"] is False


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------


class TestProbeEnvironment:
    def test_not_ready_when_ai_unconfigured(self, tmp_path, monkeypatch):
        _stub_ffmpeg(
            monkeypatch, filters_text=_FILTERS_WITH_DRAWTEXT, help_text="boxborderw"
        )
        font = tmp_path / "f.ttf"
        font.write_bytes(b"fake")
        settings.FINALCUT_FONT_FILE = str(font)

        env = probe_environment()
        assert env["ready"] is False
        assert env["ai"]["ok"] is False
        assert env["ffmpeg"]["ok"] is True
        assert env["font"]["file"] == str(font)
        assert env["default_output_dir"].endswith("finalcut")
        # 样式清单给前端渲染色块：至少三套，每套带 key/label/配色
        assert len(env["text_styles"]) == 3
        for style in env["text_styles"]:
            assert style["key"] and style["label"] and style["preview_text"]
        assert env["default_style"] == "white_box"

    def test_ready_when_all_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "AI_API_KEY", "sk-realkey123456789")
        _stub_ffmpeg(
            monkeypatch, filters_text=_FILTERS_WITH_DRAWTEXT, help_text="boxborderw"
        )
        font = tmp_path / "f.ttf"
        font.write_bytes(b"fake")
        settings.FINALCUT_FONT_FILE = str(font)
        assert probe_environment()["ready"] is True

    def test_missing_font_warns(self, monkeypatch):
        monkeypatch.setattr(settings, "AI_API_KEY", "sk-realkey123456789")
        _stub_ffmpeg(
            monkeypatch, filters_text=_FILTERS_WITH_DRAWTEXT, help_text=""
        )
        monkeypatch.setattr(finalcut_env, "_FONT_CANDIDATES", ())
        env = probe_environment()
        assert env["ready"] is False
        assert env["font"] is None
        assert any("中文字体" in w for w in env["warnings"])

    def test_old_ffmpeg_warns_about_boxborderw(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "AI_API_KEY", "sk-realkey123456789")
        _stub_ffmpeg(
            monkeypatch, filters_text=_FILTERS_WITH_DRAWTEXT, help_text="fontsize"
        )
        font = tmp_path / "f.ttf"
        font.write_bytes(b"fake")
        settings.FINALCUT_FONT_FILE = str(font)
        env = probe_environment()
        assert env["ready"] is True  # 不影响可用性
        assert any("boxborderw" in w for w in env["warnings"])
