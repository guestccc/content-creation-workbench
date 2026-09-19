"""VOICEBOX_BASE_URL 的 .env 持久化与地址规范化（voicebox_settings.py）测试。

与 test_subtitle_settings.py 同一套理由：这个模块改的是用户真实的
backend/.env，所以全部断言落在 tmp_path 里的假 .env 上
（monkeypatch 掉 voicebox_settings._ENV_PATH），一个字节都不碰真文件。

断言重点在两个地方：
1. **地址规范化**（补 scheme、去尾斜杠、非法值当场报错）—— 这是本模块独有的
   值语义，行级读写机制由 app/core/env_file.py 与其它设置共用；
2. **其余内容一字未动**（注释、空行、CRLF 原样带回），所以用 read_bytes()
   做字节级比较，而不是 read_text() —— 后者会把 CRLF 归一成 LF，正好
   掩盖我们要防的问题。
"""

import pytest

from app.services import voicebox_settings
from app.services.voicebox_settings import (
    base_url_source,
    env_var_shadowing,
    normalize_base_url,
    read_base_url,
    shadowing_warning,
    sync_from_env_file,
    write_base_url,
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
    而开发机上真有可能导出过 VOICEBOX_BASE_URL，那会盖过 .env。
    """
    monkeypatch.delenv("VOICEBOX_BASE_URL", raising=False)
    path = tmp_path / ".env"
    path.write_bytes(SAMPLE.encode("utf-8"))
    monkeypatch.setattr(voicebox_settings, "_ENV_PATH", path)
    return path


@pytest.fixture()
def fake_settings_base_url(monkeypatch):
    """挡一下 settings 单例，避免 sync_from_env_file 污染后续用例。"""
    from app.core.config import default_voicebox_base_url, settings

    monkeypatch.setattr(settings, "VOICEBOX_BASE_URL", default_voicebox_base_url())
    return settings


class TestNormalize:
    """地址规范化：用户输入五花八门，落盘前必须收成一种形状。"""

    def test_adds_scheme(self):
        assert normalize_base_url("127.0.0.1:17493") == "http://127.0.0.1:17493"

    def test_strips_trailing_slash(self):
        assert normalize_base_url("http://127.0.0.1:17493/") == "http://127.0.0.1:17493"

    def test_strips_whitespace(self):
        """粘贴过来的地址常带空格。"""
        assert normalize_base_url("  http://192.168.1.9:17493  ") == "http://192.168.1.9:17493"

    def test_keeps_https(self):
        assert normalize_base_url("https://voicebox.example.com") == "https://voicebox.example.com"

    def test_keeps_path_prefix(self):
        """反代到子路径是常见部署，不能把路径吃掉。"""
        assert normalize_base_url("http://box.lan/vb/") == "http://box.lan/vb"

    def test_empty_means_clear(self):
        """空串 = 恢复默认地址，不报错。"""
        assert normalize_base_url("") == ""
        assert normalize_base_url("   ") == ""

    def test_bad_scheme_raises(self):
        """file:// 之类的协议没有意义，宁可当场报错。"""
        with pytest.raises(ValueError):
            normalize_base_url("file:///etc/passwd")

    def test_missing_hostname_raises(self):
        with pytest.raises(ValueError):
            normalize_base_url("http://")

    def test_newline_raises(self):
        """换行必须拦住：urlparse 会把它悄悄吃掉，而写进 .env 就多出一行配置。"""
        with pytest.raises(ValueError):
            normalize_base_url("http://a.com\nEVIL=1")

    def test_inner_space_raises(self):
        with pytest.raises(ValueError):
            normalize_base_url("http://127.0.0.1:17493 extra")


class TestReadAndWrite:
    def test_missing_file_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(voicebox_settings, "_ENV_PATH", tmp_path / "nope.env")
        assert read_base_url() is None

    def test_missing_key_returns_none(self, fake_env):
        assert read_base_url() is None

    def test_empty_value_returns_empty_string(self, fake_env):
        """空串表示「显式清空」，与「键不存在」的 None 不同。"""
        with fake_env.open("a", encoding="utf-8", newline="") as handle:
            handle.write("VOICEBOX_BASE_URL=\r\n")
        assert read_base_url() == ""

    def test_write_appends_section_and_round_trips(self, fake_env):
        write_base_url("http://127.0.0.1:17493")

        text = fake_env.read_bytes().decode("utf-8")
        assert "VOICEBOX_BASE_URL=http://127.0.0.1:17493" in text
        assert "# ---------- 智能配音（Voicebox） ----------" in text
        assert read_base_url() == "http://127.0.0.1:17493"

    def test_write_preserves_other_content(self, fake_env):
        """其余内容一字未动（含 CRLF，不许被归一成 LF）。"""
        write_base_url("http://127.0.0.1:17493")

        raw = fake_env.read_bytes()
        # 原有内容按字节原样打头，新段追加在后面
        assert raw.startswith(SAMPLE.encode("utf-8"))
        assert raw != SAMPLE.encode("utf-8")

    def test_write_twice_updates_in_place(self, fake_env):
        """第二次写改的是同一行，不是再追加一行。"""
        write_base_url("http://127.0.0.1:17493")
        write_base_url("http://192.168.1.9:17493")

        text = fake_env.read_bytes().decode("utf-8")
        assert text.count("VOICEBOX_BASE_URL=") == 1
        assert read_base_url() == "http://192.168.1.9:17493"

    def test_write_empty_is_allowed(self, fake_env):
        """空串 = 恢复默认，写得进去（页面「恢复默认地址」走这条）。"""
        write_base_url("")
        assert read_base_url() == ""


class TestSource:
    def test_default_when_nothing_configured(self, fake_env, fake_settings_base_url):
        assert base_url_source() == "default"

    def test_env_file_when_written(self, fake_env, fake_settings_base_url):
        write_base_url("http://192.168.1.9:17493")
        assert base_url_source() == "env_file"

    def test_environment_wins(self, fake_env, fake_settings_base_url, monkeypatch):
        monkeypatch.setenv("VOICEBOX_BASE_URL", "http://env.example:17493")
        assert env_var_shadowing() is True
        assert base_url_source() == "environment"
        assert shadowing_warning() is not None

    def test_no_warning_when_not_shadowed(self, fake_env, fake_settings_base_url):
        assert shadowing_warning() is None


class TestSyncFromEnvFile:
    def test_syncs_value_into_settings(self, fake_env, fake_settings_base_url):
        write_base_url("http://192.168.1.9:17493")
        assert sync_from_env_file() is True
        assert fake_settings_base_url.VOICEBOX_BASE_URL == "http://192.168.1.9:17493"

    def test_second_sync_is_noop(self, fake_env, fake_settings_base_url):
        write_base_url("http://192.168.1.9:17493")
        sync_from_env_file()
        assert sync_from_env_file() is False

    def test_missing_key_falls_back_to_default(self, fake_env, fake_settings_base_url):
        """用户把那行删干净了 → 回到默认地址，而不是留着旧值。"""
        from app.core.config import default_voicebox_base_url

        fake_settings_base_url.VOICEBOX_BASE_URL = "http://stale.example:1"
        assert sync_from_env_file() is True
        assert fake_settings_base_url.VOICEBOX_BASE_URL == default_voicebox_base_url()

    def test_environment_variable_blocks_sync(self, fake_env, fake_settings_base_url, monkeypatch):
        """环境变量优先级更高：运行中的行为必须和重启后一致，所以不同步。"""
        monkeypatch.setenv("VOICEBOX_BASE_URL", "http://env.example:17493")
        write_base_url("http://192.168.1.9:17493")
        before = fake_settings_base_url.VOICEBOX_BASE_URL
        assert sync_from_env_file() is False
        assert fake_settings_base_url.VOICEBOX_BASE_URL == before
