"""envfile 模块的单元测试。

优先级规则是这里最容易出错、也最难在运行时发现的部分：
一旦 CLI 的取值顺序与 pydantic-settings 不一致，就会表现为
「界面显示的地址和应用实际监听的地址对不上」。
"""

import pytest

from cw.envfile import parse_env_file, resolve_bind
from cw.errors import ConfigError


class TestParseEnvFile:
    def test_missing_file_returns_empty(self, tmp_path):
        """backend/.env 是可选的，文件不存在不是错误。"""
        assert parse_env_file(tmp_path / "不存在") == {}

    def test_basic_key_value(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("HOST=127.0.0.1\nPORT=8000\n", encoding="utf-8")
        assert parse_env_file(path) == {"HOST": "127.0.0.1", "PORT": "8000"}

    def test_ignores_comments_and_blank_lines(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text(
            "# 这是注释\n\n   \nPORT=8000\n# 另一条注释\n",
            encoding="utf-8",
        )
        assert parse_env_file(path) == {"PORT": "8000"}

    def test_strips_export_prefix(self, tmp_path):
        """shell 里常见的 `export KEY=VALUE` 写法也要认。"""
        path = tmp_path / ".env"
        path.write_text("export PORT=9000\n", encoding="utf-8")
        assert parse_env_file(path) == {"PORT": "9000"}

    def test_strips_matching_quotes(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text(
            "A=\"双引号\"\nB='单引号'\nC=未加引号\nD=\"只有左边\n",
            encoding="utf-8",
        )
        assert parse_env_file(path) == {
            "A": "双引号",
            "B": "单引号",
            "C": "未加引号",
            "D": '"只有左边',
        }

    def test_value_may_contain_equals_sign(self, tmp_path):
        """DATABASE_URL 这类值里带等号，只能按第一个等号切分。"""
        path = tmp_path / ".env"
        path.write_text(
            "DATABASE_URL=sqlite:///./workbench.db?mode=ro&cache=1\n",
            encoding="utf-8",
        )
        assert parse_env_file(path) == {
            "DATABASE_URL": "sqlite:///./workbench.db?mode=ro&cache=1"
        }

    def test_handles_crlf_line_endings(self, tmp_path):
        path = tmp_path / ".env"
        path.write_bytes(b"PORT=8000\r\nDEBUG=true\r\n")
        assert parse_env_file(path) == {"PORT": "8000", "DEBUG": "true"}

    def test_key_is_normalized_to_uppercase(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("port=8000\n", encoding="utf-8")
        assert parse_env_file(path) == {"PORT": "8000"}

    def test_skips_lines_without_equals(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("这不是赋值语句\nPORT=8000\n", encoding="utf-8")
        assert parse_env_file(path) == {"PORT": "8000"}

    def test_later_duplicate_key_wins(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("PORT=8000\nPORT=9000\n", encoding="utf-8")
        assert parse_env_file(path) == {"PORT": "9000"}


class TestResolveBind:
    def test_no_env_file_uses_defaults(self, tmp_path):
        assert resolve_bind(tmp_path / "不存在", environ={}) == ("127.0.0.1", 8000)

    def test_none_env_file_uses_defaults(self):
        assert resolve_bind(None, environ={}) == ("127.0.0.1", 8000)

    def test_reads_from_env_file(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("HOST=0.0.0.0\nPORT=9000\n", encoding="utf-8")
        assert resolve_bind(path, environ={}) == ("0.0.0.0", 9000)

    def test_environ_overrides_env_file(self, tmp_path):
        """这是必须与 pydantic-settings 对齐的一条。

        Settings 的优先级是 os.environ > .env > 默认值。CLI 如果只读 .env，
        用户 `export PORT=9001` 时就会显示 9000，而应用内部读到 9001。
        """
        path = tmp_path / ".env"
        path.write_text("PORT=9000\n", encoding="utf-8")
        assert resolve_bind(path, environ={"PORT": "9001"}) == ("127.0.0.1", 9001)

    def test_empty_environ_value_falls_back_to_env_file(self, tmp_path):
        """空字符串视为未设置，否则 `PORT= ./cw` 会得到空值而不是回退。"""
        path = tmp_path / ".env"
        path.write_text("PORT=9000\n", encoding="utf-8")
        assert resolve_bind(path, environ={"PORT": "   "}) == ("127.0.0.1", 9000)

    def test_partial_env_file_keeps_default_for_missing_key(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("PORT=9000\n", encoding="utf-8")
        assert resolve_bind(path, environ={}) == ("127.0.0.1", 9000)

    def test_non_integer_port_raises_with_source(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("PORT=abc\n", encoding="utf-8")
        with pytest.raises(ConfigError) as excinfo:
            resolve_bind(path, environ={})
        # 错误信息要指出来源文件，否则用户不知道该改哪里
        assert str(path) in str(excinfo.value)
        assert "abc" in str(excinfo.value)

    def test_non_integer_port_from_environ_names_environ(self, tmp_path):
        with pytest.raises(ConfigError) as excinfo:
            resolve_bind(tmp_path / "不存在", environ={"PORT": "abc"})
        assert "环境变量" in str(excinfo.value)

    @pytest.mark.parametrize("bad_port", ["0", "-1", "65536", "99999"])
    def test_out_of_range_port_raises(self, tmp_path, bad_port):
        path = tmp_path / ".env"
        path.write_text(f"PORT={bad_port}\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            resolve_bind(path, environ={})

    def test_boundary_ports_are_accepted(self, tmp_path):
        path = tmp_path / ".env"
        path.write_text("PORT=1\n", encoding="utf-8")
        assert resolve_bind(path, environ={})[1] == 1
        path.write_text("PORT=65535\n", encoding="utf-8")
        assert resolve_bind(path, environ={})[1] == 65535
