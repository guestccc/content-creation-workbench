"""日志读取。

只提供「读尾部」这一种操作——查看日志场景里用户关心的是最近发生了什么，
整个文件灌进终端既没有意义也会拖慢启动。
"""

from __future__ import annotations

from pathlib import Path

# 默认读取的尾部字节数。按行算不可靠（vite 的一行编译输出可能非常长），
# 按字节截断再按行重组是更稳的做法
DEFAULT_TAIL_BYTES = 64 * 1024


def tail_text(path: Path, max_bytes: int = DEFAULT_TAIL_BYTES) -> str:
    """读取日志文件末尾的一段文本。

    - 文件不存在返回空串（调用方自己决定怎么展示「还没有日志」）；
    - 从中段截断时丢掉第一个不完整的行；
    - 末尾有非法 UTF-8 时用 replacement 字符兜底，不让日志查看功能崩溃。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return ""

    if size == 0:
        return ""

    try:
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            raw = f.read()
    except OSError:
        return ""

    text = raw.decode("utf-8", errors="replace")

    # 中段截断时第一行是不完整的，丢掉它比显示半截乱码更清楚
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]

    return text


def tail_lines(path: Path, count: int = 50) -> list[str]:
    """读取日志的最后 N 行。"""
    text = tail_text(path)
    if not text:
        return []
    lines = text.splitlines()
    return lines[-count:] if len(lines) > count else lines
