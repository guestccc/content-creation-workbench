"""解析 .env 文件并确定后端监听的地址。

这里刻意只处理简单场景：backend 用的是 pydantic-settings，它只认
KEY=VALUE 形式，不做多行值和变量插值。CLI 保持同样的解析范围，
避免「CLI 认得的语法应用不认」这种不对称。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from cw.errors import ConfigError

# 取值优先级里用到的来源标签，出错时告诉用户值是从哪来的
_SOURCE_ENVIRON = "环境变量"
_SOURCE_DEFAULT = "默认值"


def parse_env_file(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 形式的 .env 文件。

    处理：注释行、空行、`export ` 前缀、单/双引号包裹、CRLF 行尾、值里含等号。
    文件不存在时返回空字典——这是正常情况，backend/.env 是可选的。
    """
    if not path.is_file():
        return {}

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}：{exc}") from exc

    values: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # `export PORT=8000` 这种写法在 shell 里很常见，也一并接受
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        key, sep, value = line.partition("=")
        if not sep:
            # 没有等号的行不是赋值语句，跳过（pydantic-settings 同样忽略）
            continue

        key = key.strip().upper()
        if not key:
            continue

        values[key] = _strip_quotes(value.strip())

    return values


def _strip_quotes(value: str) -> str:
    """去掉成对的包裹引号。

    只做去引号，不做转义展开：双引号内的 \\n 之类保持字面量，
    与「不做变量插值」是同一个取舍。
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _lookup(
    name: str,
    environ: Mapping[str, str],
    file_values: Mapping[str, str],
    env_file: Path | None,
) -> tuple[str | None, str]:
    """按 `os.environ > .env 文件` 取值，返回 (值, 来源描述)。

    空字符串视为未设置——否则 `PORT= ./cw` 这类写法会得到一个空值而不是回退。
    """
    raw = environ.get(name)
    if raw is not None and raw.strip():
        return raw.strip(), _SOURCE_ENVIRON

    raw = file_values.get(name)
    if raw is not None and raw.strip():
        return raw.strip(), (str(env_file) if env_file else ".env 文件")

    return None, _SOURCE_DEFAULT


def resolve_bind(
    env_file: Path | None,
    environ: Mapping[str, str] | None = None,
    default_host: str = "127.0.0.1",
    default_port: int = 8000,
) -> tuple[str, int]:
    """确定后端监听的 (host, port)。

    优先级严格照搬 pydantic-settings 的行为：`os.environ > .env 文件 > 默认值`。
    必须一致——否则用户 `export PORT=8001` 时，CLI 会显示 .env 里的 8000，
    而应用内部 settings.PORT 读到 8001，排查问题时会被这个错位带偏。

    host 不做合法性校验（uvicorn 自己会报），port 不合法则直接抛错并指出来源。
    """
    env = os.environ if environ is None else environ
    file_values = parse_env_file(env_file) if env_file is not None else {}

    host_raw, _ = _lookup("HOST", env, file_values, env_file)
    host = host_raw or default_host

    port_raw, port_source = _lookup("PORT", env, file_values, env_file)
    if port_raw is None:
        return host, default_port

    try:
        port = int(port_raw)
    except ValueError:
        raise ConfigError(
            f"{port_source} 中设置的 PORT 不是整数：{port_raw!r}"
        ) from None

    if not 1 <= port <= 65535:
        raise ConfigError(f"{port_source} 中设置的 PORT 超出 1-65535 范围：{port}")

    return host, port
