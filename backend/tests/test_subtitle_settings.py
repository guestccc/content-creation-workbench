"""SUBTITLE_VC_ROOT 的 .env 持久化（subtitle_settings.py）测试。

这个模块干的是「改用户 .env 文件」这种破坏性操作，所以测试全部落在
tmp_path 里的假 .env 上（monkeypatch 掉 subtitle_settings._ENV_PATH），
一个字节都不碰真的 backend/.env。

断言重点不是「写进去了」，而是**其余内容一字未动**：注释、空行、换行符风格、
BOM 都必须在往返后保持原样。所以这里大量用 read_bytes() 做字节级比较，
而不是 read_text() —— 后者会把 CRLF 悄悄归一成 LF，正好掩盖我们要防的问题。
"""

from pathlib import Path

import pytest

from app.services import subtitle_settings
from app.services.subtitle_settings import (
    env_var_shadowing,
    normalize_vc_root,
    read_vc_root,
    shadowing_warning,
    vc_root_source,
    write_vc_root,
)

#: 一个具有真实 .env 全部特征的样本：注释、空行、CRLF、带段头的分段。
SAMPLE = (
    "# 环境变量样例文件\r\n"
    "# 使用方法：复制为 .env 后按需修改。\r\n"
    "\r\n"
    "# ---------- 应用信息 ----------\r\n"
    "APP_NAME=内容创作工作台\r\n"
    "\r\n"
    "# ---------- 日志 ----------\r\n"
    "LOG_LEVEL=INFO\r\n"
)


@pytest.fixture()
def fake_env(monkeypatch, tmp_path):
    """把 .env 指到 tmp 下的假文件，并挡住真实环境变量。

    挡住环境变量是必须的：这些用例断言的是「.env 这一层」的行为，
    而开发机上真有可能导出过 SUBTITLE_VC_ROOT，那会盖过 .env。
    """
    monkeypatch.delenv("SUBTITLE_VC_ROOT", raising=False)
    path = tmp_path / ".env"
    path.write_bytes(SAMPLE.encode("utf-8"))
    monkeypatch.setattr(subtitle_settings, "_ENV_PATH", path)
    return path


class TestRead:
    """read_vc_root 对 .env 各种写法的解析。"""

    def test_missing_file_returns_none(self, monkeypatch, tmp_path):
        """文件不存在时返回 None（区分于「键存在但为空」）。"""
        monkeypatch.setattr(subtitle_settings, "_ENV_PATH", tmp_path / "nope.env")
        assert read_vc_root() is None

    def test_missing_key_returns_none(self, fake_env):
        """样本文件里没有这个键。"""
        assert read_vc_root() is None

    def test_commented_line_is_not_a_value(self, fake_env):
        """`# SUBTITLE_VC_ROOT=...` 是文档样例，不算配置。"""
        with fake_env.open("a", encoding="utf-8", newline="") as handle:
            handle.write("# SUBTITLE_VC_ROOT=/should/not/win\r\n")
        assert read_vc_root() is None

    def test_bare_value(self, fake_env):
        """裸值原样读出（反斜杠不转义，Windows 路径安全）。"""
        with fake_env.open("a", encoding="utf-8", newline="") as handle:
            handle.write("SUBTITLE_VC_ROOT=E:\\带货\\工具\\VC\r\n")
        assert read_vc_root() == "E:\\带货\\工具\\VC"

    def test_single_quoted_value(self, fake_env):
        """单引号是字面量，引号本身不算值的一部分。"""
        with fake_env.open("a", encoding="utf-8", newline="") as handle:
            handle.write("SUBTITLE_VC_ROOT='/path/with space'\r\n")
        assert read_vc_root() == "/path/with space"

    def test_inline_comment_is_stripped(self, fake_env):
        """未加引号时 ` #` 之后算行内注释。"""
        with fake_env.open("a", encoding="utf-8", newline="") as handle:
            handle.write("SUBTITLE_VC_ROOT=/opt/vc  # 我装在这\r\n")
        assert read_vc_root() == "/opt/vc"

    def test_empty_value_returns_empty_string(self, fake_env):
        """空串表示「显式清空」，与「键不存在」的 None 不同。"""
        with fake_env.open("a", encoding="utf-8", newline="") as handle:
            handle.write("SUBTITLE_VC_ROOT=\r\n")
        assert read_vc_root() == ""

    def test_export_prefix(self, fake_env):
        """dotenv 认 `export` 前缀。"""
        with fake_env.open("a", encoding="utf-8", newline="") as handle:
            handle.write("export SUBTITLE_VC_ROOT=/opt/vc\r\n")
        assert read_vc_root() == "/opt/vc"


class TestWrite:
    """write_vc_root 的三个分支与保真性。"""

    def test_appends_section_after_last_line(self, fake_env):
        """没有同名行也没有段头 → 文件尾追加整段。"""
        write_vc_root("E:\\带货\\工具\\VideoCaptioner-master")

        raw = fake_env.read_bytes()
        text = raw.decode("utf-8")
        assert "SUBTITLE_VC_ROOT=E:\\带货\\工具\\VideoCaptioner-master" in text
        assert "# ---------- 视频字幕提取（VideoCaptioner） ----------" in text
        assert read_vc_root() == "E:\\带货\\工具\\VideoCaptioner-master"

    def test_preserves_comments_and_other_keys(self, fake_env):
        """其余内容一字未动。"""
        write_vc_root("/opt/vc")

        text = fake_env.read_bytes().decode("utf-8")
        assert "# 环境变量样例文件" in text
        assert "# 使用方法：复制为 .env 后按需修改。" in text
        assert "APP_NAME=内容创作工作台" in text
        assert "LOG_LEVEL=INFO" in text
        assert "# ---------- 应用信息 ----------" in text

    def test_preserves_crlf_and_does_not_add_bom(self, fake_env):
        """换行符风格不变，且不主动加 BOM（BOM 会坏掉第一个键名）。"""
        write_vc_root("/opt/vc")

        raw = fake_env.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        # 除最后一行外每行都该以 CRLF 结尾；不得出现裸 LF
        assert raw.count(b"\n") == raw.count(b"\r\n") + (0 if raw.endswith(b"\n") else 1)

    def test_replaces_existing_key_in_place(self, fake_env):
        """已有未注释同名行 → 原地替换，不新增段。"""
        write_vc_root("/first/vc")
        write_vc_root("/second/vc")

        text = fake_env.read_bytes().decode("utf-8")
        assert "/first/vc" not in text
        assert read_vc_root() == "/second/vc"

    def test_second_write_does_not_duplicate_section(self, fake_env):
        """连续写入不产生重复段头（否则文件会越长越乱）。"""
        write_vc_root("/a")
        write_vc_root("/b")
        write_vc_root("/c")

        text = fake_env.read_bytes().decode("utf-8")
        assert text.count("# ---------- 视频字幕提取（VideoCaptioner） ----------") == 1
        assert text.count("SUBTITLE_VC_ROOT=") == 1

    def test_inserts_after_existing_section_header(self, fake_env):
        """已有「视频字幕提取」段头 → 插到它后面，而不是追加到文件尾。"""
        with fake_env.open("a", encoding="utf-8", newline="") as handle:
            handle.write(
                "\r\n# ---------- 视频字幕提取（VideoCaptioner） ----------\r\n"
                "# SUBTITLE_VC_ROOT=/样例，注释掉的\r\n"
            )
        write_vc_root("/opt/vc")

        lines = fake_env.read_bytes().decode("utf-8").splitlines()
        header = lines.index("# ---------- 视频字幕提取（VideoCaptioner） ----------")
        assert lines[header + 1] == "SUBTITLE_VC_ROOT=/opt/vc"
        # 注释掉的样例行必须原样还在
        assert "# SUBTITLE_VC_ROOT=/样例，注释掉的" in lines

    def test_section_header_match_is_lenient(self, fake_env):
        """段头横线数量被手改过也要认出来，别又追加一个段。"""
        with fake_env.open("a", encoding="utf-8", newline="") as handle:
            handle.write("\r\n# ----- 视频字幕提取（VideoCaptioner） -----\r\n")
        write_vc_root("/opt/vc")

        text = fake_env.read_bytes().decode("utf-8")
        assert text.count("视频字幕提取") == 1
        assert read_vc_root() == "/opt/vc"

    def test_comments_out_duplicate_keys(self, fake_env):
        """重复的同名行里，dotenv 是后行覆盖前行 —— 只改第一条会失效。"""
        with fake_env.open("a", encoding="utf-8", newline="") as handle:
            handle.write("SUBTITLE_VC_ROOT=/old/one\r\nSUBTITLE_VC_ROOT=/old/two\r\n")
        write_vc_root("/new")

        text = fake_env.read_bytes().decode("utf-8")
        assert "SUBTITLE_VC_ROOT=/new" in text
        # 两条旧的都不能再是生效行
        assert "\nSUBTITLE_VC_ROOT=/old" not in "\n" + text
        assert read_vc_root() == "/new"

    def test_empty_value_resets_to_auto(self, fake_env):
        """空串 = 恢复自动探测。"""
        write_vc_root("/opt/vc")
        write_vc_root("")
        assert read_vc_root() == ""

    def test_bom_is_preserved(self, monkeypatch, tmp_path):
        """用户用某些 Windows 编辑器加了 BOM，不能悄悄抹掉。"""
        path = tmp_path / ".env"
        path.write_bytes(b"\xef\xbb\xbf" + SAMPLE.encode("utf-8"))
        monkeypatch.setattr(subtitle_settings, "_ENV_PATH", path)

        write_vc_root("/opt/vc")
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert raw.count(b"\xef\xbb\xbf") == 1

    def test_quotes_value_containing_hash(self, fake_env):
        """含 `#` 的路径必须加引号，否则会被当成行内注释截断。"""
        write_vc_root("/opt/vc#2")
        assert read_vc_root() == "/opt/vc#2"

    def test_leaves_no_temp_file(self, fake_env, tmp_path):
        """原子写的临时文件不能残留。"""
        write_vc_root("/opt/vc")
        assert list(tmp_path.glob(".env.*.local")) == []

    def test_file_created_when_missing(self, monkeypatch, tmp_path):
        """原本没有 .env 时能建出来。"""
        path = tmp_path / ".env"
        monkeypatch.setattr(subtitle_settings, "_ENV_PATH", path)
        write_vc_root("/opt/vc")
        assert read_vc_root() == "/opt/vc"


class TestValueSafety:
    """写不进去的值要明确拒绝，而不是写坏用户的配置文件。"""

    @pytest.mark.parametrize(
        "bad",
        [
            "/a\nSUBTITLE_WORKER_ENABLED=false",  # 配置注入
            "/a\rb",
            "/a'b",
            '/a"b',
            "/a\x00b",
        ],
    )
    def test_rejects_dangerous_characters(self, fake_env, bad):
        with pytest.raises(ValueError):
            write_vc_root(bad)

    def test_rejected_value_does_not_touch_file(self, fake_env):
        """拒绝之后文件必须原封不动。"""
        before = fake_env.read_bytes()
        with pytest.raises(ValueError):
            write_vc_root("/a\nINJECTED=1")
        assert fake_env.read_bytes() == before


class TestSource:
    """vc_root_source / env_var_shadowing 的判定。"""

    def test_auto_when_nothing_configured(self, fake_env):
        assert vc_root_source() == "auto"

    def test_env_file_when_key_present(self, fake_env):
        write_vc_root("/opt/vc")
        assert vc_root_source() == "env_file"

    def test_auto_when_key_explicitly_empty(self, fake_env):
        """空串归 auto，与「恢复自动探测」的语义对齐。"""
        write_vc_root("")
        assert vc_root_source() == "auto"

    def test_environment_wins_over_env_file(self, fake_env, monkeypatch):
        """环境变量优先级高于 .env。"""
        write_vc_root("/from/env/file")
        monkeypatch.setenv("SUBTITLE_VC_ROOT", "/from/process")
        assert env_var_shadowing() is True
        assert vc_root_source() == "environment"

    def test_shadowing_detection_is_case_insensitive(self, fake_env, monkeypatch):
        """pydantic-settings 配了 case_sensitive=False，小写导出也算数。"""
        monkeypatch.setenv("subtitle_vc_root", "/from/process")
        assert env_var_shadowing() is True

    def test_no_shadowing_warning_when_not_set(self, fake_env):
        assert shadowing_warning() is None

    def test_shadowing_warning_explains_restart_behavior(self, fake_env, monkeypatch):
        """提醒必须说清「本次生效、重启后失效」——否则用户只会觉得设置丢了。"""
        monkeypatch.setenv("SUBTITLE_VC_ROOT", "/from/process")
        text = shadowing_warning()
        assert text is not None
        assert "SUBTITLE_VC_ROOT" in text
        assert "环境变量" in text
        assert "重启" in text


class TestNormalize:
    """路径规范化。"""

    def test_strips_whitespace(self):
        assert Path(normalize_vc_root("  /opt/vc  ")) == Path("/opt/vc").resolve()

    def test_empty_stays_empty(self):
        assert normalize_vc_root("") == ""
        assert normalize_vc_root("   ") == ""

    def test_expands_tilde(self):
        result = normalize_vc_root("~/vc")
        assert "~" not in result
        assert result.endswith("vc")
