#!/bin/sh
# 内容创作工作台 - 开发服务管理 CLI 的启动器。
#
# 直接执行 cli/.venv 里的可执行文件，而不是 `uv run --project cli cw`：
# 后者每次都会做一遍环境校验，lock 过期时还会尝试联网。
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_BIN="$ROOT/cli/.venv/bin/cw"

if [ ! -x "$VENV_BIN" ]; then
    # 首次运行，或依赖有变动导致可执行文件被清理时，自动准备一次环境。
    # 依赖版本变更后 shim 依然存在、不会触发这里，需要手动执行：
    #   uv sync --project cli
    echo "首次运行：正在准备 CLI 环境（需要联网）..." >&2
    uv sync --project "$ROOT/cli" --quiet
fi

exec "$VENV_BIN" "$@"
