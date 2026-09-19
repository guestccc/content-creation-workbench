"""一键成品的烧字排版：上屏样式模板、换行与字号估算、drawtext 滤镜串构造。

样式表的每条既是一份 drawtext 参数集（后端烧字用），也是一份前端预览配色
（`preview_*` 字段给色块用）。两边从同一份数据渲染，不会出现「页面上看着
是黄字、烧出来是白字」的漂移。

排版策略（与 ffmpeg drawtext 的分工）：
- **本模块只决定「字号与换行点」**：按估算字宽（CJK≈1.0em、ASCII≈0.55em）
  贪心折行，从大到小试字号，第一个竖向放得下的胜出；
- **居中交给 ffmpeg 实测值**：滤镜串用 `x=<cx>-text_w/2:y=<cy>-text_h/2`，
  text_w/text_h 是 drawtext 运行时量出来的真实排版尺寸 —— 估算的误差只
  表现为向内/向外溢出，不会出现偏右；
- **转义用「临时目录 + ASCII 相对路径 + cwd」绕开**：滤镜串里只有
  `fontfile=font.ttf:textfile=copy.txt`，字体与文案在跑任务时复制/写入
  任务临时目录，ffmpeg 以 cwd=临时目录 启动。这样彻底不碰 Windows 盘符的
  `:` 与 `\` 转义（本仓库路径含中文，已经踩过坑）。
"""

import math
import shutil
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from app.core.config import settings


@dataclass(frozen=True)
class TextStyle:
    """一套上屏样式。

    drawtext 参数字段直接拼滤镜串；preview_* 只给前端色块用。

    Attributes:
        key: 模板标识（进任务记录、进 URL 参数，定了就不改）。
        label: 给人看的名字。
        fontcolor / bordercolor: drawtext 颜色（支持 white、0xRRGGBB@alpha 等）。
        borderw: 描边宽度（px）。
        box: 是否画半透明底（drawtext box=1）。
        boxcolor: 底色（带透明度，如 black@0.45）；box=False 时忽略。
        boxborderw: 底色外扩像素（drawtext 5.x 起支持；不支持就省略该参数，
            由构建滤镜串的一方按探测到的能力决定带不带）。
        line_spacing: 行距（px）。
        preview_text / preview_background: 前端色块的文字色与底色（CSS）。
    """

    key: str
    label: str
    fontcolor: str
    borderw: int
    bordercolor: str
    box: bool
    boxcolor: str = ""
    boxborderw: int = 0
    line_spacing: int = 8
    preview_text: str = "#ffffff"
    preview_background: str = "rgba(0, 0, 0, 0.45)"


#: 三套可切换的上屏样式。DEFAULT_STYLE 是用户不选时的默认。
#: 模板只有三套是刻意的：参数收敛在第一版交互里，任意配色留给后续。
WHITE_BOX = TextStyle(
    key="white_box",
    label="白字黑边带底",
    fontcolor="white",
    borderw=2,
    bordercolor="black",
    box=True,
    boxcolor="black@0.45",
    boxborderw=12,
    preview_text="#ffffff",
    preview_background="rgba(0, 0, 0, 0.45)",
)
YELLOW = TextStyle(
    key="yellow",
    label="黄字黑边",
    fontcolor="yellow",
    borderw=3,
    bordercolor="black",
    box=False,
    preview_text="#ffe95c",
    preview_background="rgba(0, 0, 0, 0.25)",
)
OUTLINE = TextStyle(
    key="outline",
    label="无底纯描边",
    fontcolor="white",
    borderw=3,
    bordercolor="black@0.9",
    box=False,
    preview_text="#ffffff",
    preview_background="rgba(0, 0, 0, 0)",
)

TEXT_STYLES: Dict[str, TextStyle] = {
    style.key: style for style in (WHITE_BOX, YELLOW, OUTLINE)
}

#: 默认样式：带货场景里「白字 + 半透明底」在复杂画面上的可读性最稳。
DEFAULT_STYLE = WHITE_BOX.key


def style_catalog() -> List[dict]:
    """给前端环境自检用的样式清单（key / label / 预览配色）。"""
    return [
        {
            "key": style.key,
            "label": style.label,
            "preview_text": style.preview_text,
            "preview_background": style.preview_background,
            "default": style.key == DEFAULT_STYLE,
        }
        for style in TEXT_STYLES.values()
    ]


def get_style(key: str) -> TextStyle:
    """按 key 取样式；未知 key 是调用方校验失职，直接抛（不静默兜底）。"""
    try:
        return TEXT_STYLES[key]
    except KeyError:
        raise ValueError(
            f"未知的文案样式：{key}（可选：{', '.join(TEXT_STYLES)}）"
        ) from None


def style_as_dict(style: TextStyle) -> dict:
    """TextStyle 的 dict 形态（落库 / 进 JSON 响应用）。"""
    return asdict(style)


# --------------------------------------------------------------------------
# 排版估算：换行、字号自适应、坐标换算
# --------------------------------------------------------------------------

#: 自动字号的尝试范围（px，相对视频实际分辨率）。
AUTO_MIN_FONT_SIZE = 16
AUTO_MAX_FONT_SIZE = 64

#: 竖向高度的松弛系数：drawtext 以框中心居中（text_h 实测），估算误差导致的
#: 轻微溢出会对称分摊到框的上下，视觉上可接受；这也是「估算 + 可调」策略的一部分。
_HEIGHT_SLACK = 1.15


def estimate_char_width(ch: str, font_size: float) -> float:
    """单个字符的估算宽度（px）：CJK/全角 ≈ 1.0em，ASCII 等 ≈ 0.55em。

    只是估算（真实宽度由 drawtext 的 text_w 实测），用来决定换行点。
    """
    east_asian = unicodedata.east_asian_width(ch)
    return font_size * (1.0 if east_asian in ("W", "F", "A") else 0.55)


def wrap_text(text: str, box_w_px: float, font_size: int) -> List[str]:
    """把文案按框宽贪心折行（尊重原文的 \\n，空行丢弃）。

    Returns:
        折行后的行列表；text 非空时至少一行。
    """
    lines: List[str] = []
    for raw in (text or "").split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        current = ""
        width = 0.0
        for ch in raw:
            char_w = estimate_char_width(ch, font_size)
            if current and width + char_w > box_w_px:
                lines.append(current)
                current, width = ch, char_w
            else:
                current += ch
                width += char_w
        if current:
            lines.append(current)
    return lines


def fit_font_size(
    text: str,
    box_w_px: float,
    box_h_px: float,
    *,
    min_size: int = AUTO_MIN_FONT_SIZE,
    max_size: int = AUTO_MAX_FONT_SIZE,
    line_spacing: int = 8,
) -> Tuple[int, List[str], bool]:
    """自动字号：从大到小逐档试，取第一个竖向放得下的字号。

    判定式：`行数×字号 + 行距×(行数-1) ≤ 框高 × 1.15`（松弛系数的理由见
    _HEIGHT_SLACK）。降到 min_size 仍放不下就截断到放得下的行数。

    Returns:
        (字号, 折行结果, 是否发生了截断)。截断原因由调用方写进 item 的
        error_message（任务本身仍然成功）。
    """
    for size in range(max_size, min_size - 1, -4):
        lines = wrap_text(text, box_w_px, size)
        if not lines:
            break
        total_h = len(lines) * size + line_spacing * (len(lines) - 1)
        if total_h <= box_h_px * _HEIGHT_SLACK:
            return size, lines, False

    lines = wrap_text(text, box_w_px, min_size)
    if not lines:
        return min_size, [""], False
    max_lines = max(
        1, math.floor((box_h_px * _HEIGHT_SLACK + line_spacing) / (min_size + line_spacing))
    )
    if len(lines) > max_lines:
        return min_size, lines[:max_lines], True
    return min_size, lines, False


def box_to_pixels(box: dict, video_spec: dict) -> Tuple[int, int, int, int]:
    """归一化框（0-1）× 显示尺寸 → 像素框 (x, y, w, h)。

    video_spec 是创建任务时固化的显示尺寸快照（probe_video_spec 已在
    ±90/270 旋转时交换过宽高），与浏览器预览闭环。
    """
    width = int(video_spec["width"])
    height = int(video_spec["height"])
    x = round(float(box["x"]) * width)
    y = round(float(box["y"]) * height)
    w = round(float(box["w"]) * width)
    h = round(float(box["h"]) * height)
    return x, y, max(1, w), max(1, h)


# --------------------------------------------------------------------------
# drawtext 滤镜串与烧字 argv
# --------------------------------------------------------------------------


def build_drawtext_filter(
    px_box: Tuple[int, int, int, int],
    font_size: int,
    style: TextStyle,
    *,
    supports_boxborderw: bool,
) -> str:
    """拼 drawtext 滤镜串。fontfile/textfile 只用 ASCII 相对名（cwd 见模块头）。

    居中表达式里的 text_w/text_h 是 drawtext 运行时实测的排版尺寸，
    Python 侧的估算误差不会表现为偏移。
    """
    x, y, w, h = px_box
    cx, cy = x + w // 2, y + h // 2
    parts = [
        "drawtext=fontfile=font.ttf",
        "textfile=copy.txt",
        # expansion=none：广告文案里出现裸 %（「立减100%」）是常态，而 drawtext
        # 默认对 textfile 内容做 %{...} 展开 —— 裸 % 只会 warning 一句
        # 「Stray %」然后把整条文案渲成空白（真机踩过）。关掉展开，% { } 全是字面量。
        "expansion=none",
        f"fontsize={font_size}",
        f"x={cx}-text_w/2",
        f"y={cy}-text_h/2",
        f"fontcolor={style.fontcolor}",
        f"borderw={style.borderw}",
        f"bordercolor={style.bordercolor}",
        f"line_spacing={style.line_spacing}",
    ]
    if style.box:
        parts.append("box=1")
        parts.append(f"boxcolor={style.boxcolor}")
        # boxborderw 是 drawtext 5.x 才加的参数，按探测到的能力决定带不带
        if supports_boxborderw and style.boxborderw:
            parts.append(f"boxborderw={style.boxborderw}")
    return ":".join(parts)


def write_drawtext_files(tmp_dir: Path, font_src: Path, text: str) -> Tuple[str, str]:
    """把字体与预折行的文案写进任务临时目录，返回滤镜串引用的相对文件名。

    - 字体复制成 `font.ttf`（已存在且大小一致就跳过，一个任务多条成片复用）；
    - 文案写成 `copy.txt`，**UTF-8 无 BOM**（带 BOM 在部分构建上会渲成方框）。
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    font_dst = tmp_dir / "font.ttf"
    if not (
        font_dst.is_file() and font_dst.stat().st_size == font_src.stat().st_size
    ):
        shutil.copyfile(font_src, font_dst)
    copy_path = tmp_dir / "copy.txt"
    copy_path.write_text(text, encoding="utf-8")  # write_text 的 utf-8 不落 BOM
    return font_dst.name, copy_path.name


def build_render_argv(
    ffmpeg: str,
    src: Path,
    dst: Path,
    filter_str: str,
    progress_path: Path,
    *,
    audio_codec: str,
) -> List[str]:
    """单条成片的烧字 argv：视频重编码 + drawtext，音频按编码分支。

    音频分支（不能无脑 `-c:a copy`）：混剪成片一定是 aac，但任意本地
    mkv/mov 可能带 vorbis/pcm/ac3，塞进 mp4 会让 muxer 报错或产废片：
    - aac/mp3 → 流复制；
    - 其它编码 → 转 aac 128k；
    - 无音轨 → -an。
    统一 `-map 0:v:0 -map 0:a:0?`，把字幕/数据流丢掉。
    """
    argv = [
        ffmpeg, "-hide_banner", "-nostats", "-y",
        "-i", str(src),
        "-vf", filter_str,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", settings.FINALCUT_RENDER_PRESET,
        "-crf", str(settings.FINALCUT_RENDER_CRF),
        "-pix_fmt", "yuv420p",
    ]
    codec = (audio_codec or "").strip().lower()
    if codec in ("aac", "mp3"):
        argv += ["-c:a", "copy"]
    elif codec:
        argv += ["-c:a", "aac", "-b:a", "128k"]
    else:
        argv += ["-an"]
    argv += [
        "-movflags", "+faststart",
        "-progress", str(progress_path),
        str(dst),
    ]
    return argv
