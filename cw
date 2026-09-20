#!/bin/sh
# content-creation-workbench - 开发服务管理 CLI 的启动器。
#
# 直接执行 cli/.venv 里的可执行文件，而不是 `uv run --project cli cw`：
# 后者每次都会做一遍环境校验，lock 过期时还会尝试联网。
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# uv 在 POSIX 上把入口装到 .venv/bin/，Windows 上装到 .venv/Scripts/。
find_cw() {
    if [ -x "$ROOT/cli/.venv/bin/cw" ]; then
        echo "$ROOT/cli/.venv/bin/cw"
    elif [ -x "$ROOT/cli/.venv/Scripts/cw.exe" ]; then
        echo "$ROOT/cli/.venv/Scripts/cw.exe"
    else
        echo ""
    fi
}

VENV_BIN="$(find_cw)"

if [ -z "$VENV_BIN" ]; then
    # 首次运行，或依赖有变动导致可执行文件被清理时，自动准备一次环境。
    # 依赖版本变更后 shim 依然存在、不会触发这里，需要手动执行：
    #   uv sync --project cli
    echo "首次运行：正在准备 CLI 环境（需要联网）..." >&2
    uv sync --project "$ROOT/cli" --quiet
    VENV_BIN="$(find_cw)"
fi

# 可编辑安装（uv 写入的 .pth）在「Windows + 非 ASCII 路径」下会失效：
# Python 按本地编码（GBK）读 .pth，而 uv 用 UTF-8 写中文路径，结果被读成乱码，
# cli/src 永远进不了 sys.path，`import cw` 报 ModuleNotFoundError。
# 这里显式把 cli/src 塞进 PYTHONPATH，跨平台都可靠。
case "$(uname -s 2>/dev/null || echo)" in
    MINGW*|MSYS*|CYGWIN*)
        # Windows：Python 用 `;` 分隔 PYTHONPATH，路径需用盘符形式（E:/…）
        SRC_PATH="$(cygpath -m "$ROOT/cli/src" 2>/dev/null || printf '%s' "$ROOT/cli/src")"
        SEP=';'
        ;;
    *)
        SRC_PATH="$ROOT/cli/src"
        SEP=':'
        ;;
esac
export PYTHONPATH="$SRC_PATH${PYTHONPATH:+$SEP$PYTHONPATH}"

# 强制 UTF-8 输出：Windows 上 Python 默认按 GBK 写 stdout，和 Git Bash 的
# UTF-8 终端不匹配，中文会显示成乱码。macOS / Linux 上这行是空操作。
export PYTHONUTF8=1

exec "$VENV_BIN" "$@"
