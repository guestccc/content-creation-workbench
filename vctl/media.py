"""
音视频信息读取。

目前只做一件事：拿视频的宽高。擦除硬字幕需要把「相对比例选区」换算成
「绝对像素坐标」——video-subtitle-remover 的命令行只接受像素坐标，
而它图形界面里存的默认值是比例，所以必须自己换算。

优先用 ffprobe；没有 ffprobe 时回退到解析 ffmpeg 的输出。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .env import FFMPEG_BIN_DIR


def find_tool(name: str) -> str | None:
    """在 PATH 和工具箱自带的 ffmpeg-bin 里找可执行文件。"""
    found = shutil.which(name)
    if found:
        return found
    bundled = FFMPEG_BIN_DIR / name
    if bundled.exists():
        return str(bundled)
    return None


def video_size(path: str | Path) -> tuple[int, int] | None:
    """读取视频的 (宽, 高)。

    Args:
        path: 视频文件路径。
    Returns:
        (宽, 高)；读取失败返回 None。
    """
    source = str(path)

    ffprobe = find_tool("ffprobe")
    if ffprobe:
        try:
            out = subprocess.run(
                [
                    ffprobe,
                    "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "csv=p=0:s=x",
                    source,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if out.returncode == 0:
                text = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
                # 形如 "1080x1920"，也可能带尾巴（帧率之类）
                match = re.match(r"^(\d+)x(\d+)", text)
                if match:
                    return int(match.group(1)), int(match.group(2))
        except (OSError, subprocess.SubprocessError, IndexError, ValueError):
            pass

    # 回退：让 ffmpeg 打印视频信息再从文本里抠
    ffmpeg = find_tool("ffmpeg")
    if ffmpeg:
        try:
            out = subprocess.run(
                [ffmpeg, "-i", source, "-hide_banner"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            # ffmpeg 把信息打在 stderr 上，形如 "Stream #0:0: Video: h264, ..., 1080x1920"
            match = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", out.stderr)
            if match:
                return int(match.group(1)), int(match.group(2))
        except (OSError, subprocess.SubprocessError):
            pass

    return None


def duration_seconds(path: str | Path) -> float | None:
    """读取媒体时长（秒）。失败返回 None。"""
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if out.returncode == 0 and out.stdout.strip():
            return float(out.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def ratio_to_pixels(
    ratio: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    """把相对比例选区换算成绝对像素选区。

    比例的顺序与 video-subtitle-remover 一致：``(ymin, ymax, xmin, xmax)``，
    取值范围 0-1，左上角为原点。

    Args:
        ratio: (ymin, ymax, xmin, xmax)，0-1 之间的小数。
        width: 视频宽度（像素）。
        height: 视频高度（像素）。
    Returns:
        (ymin, ymax, xmin, xmax)，绝对像素坐标，已做边界裁剪。
    """
    ymin_r, ymax_r, xmin_r, xmax_r = ratio

    ymin = max(0, min(int(round(ymin_r * height)), height))
    ymax = max(0, min(int(round(ymax_r * height)), height))
    xmin = max(0, min(int(round(xmin_r * width)), width))
    xmax = max(0, min(int(round(xmax_r * width)), width))

    # 保证 min < max，否则下游画不出掩码
    if ymax <= ymin:
        ymax = min(height, ymin + 1)
    if xmax <= xmin:
        xmax = min(width, xmin + 1)

    return ymin, ymax, xmin, xmax


def parse_ratio(text: str) -> tuple[float, float, float, float] | None:
    """解析 ``"0.88,0.99,0.15,0.85"`` 形式的比例选区。

    兼容用空格或分号分隔的写法。解析失败返回 None。
    """
    if not text:
        return None
    parts = re.split(r"[,;\s]+", text.strip())
    if len(parts) != 4:
        return None
    try:
        values = tuple(float(part) for part in parts)
    except ValueError:
        return None
    if any(value < 0 or value > 1 for value in values):
        return None
    return values  # type: ignore[return-value]
