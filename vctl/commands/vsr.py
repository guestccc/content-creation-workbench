"""
video-subtitle-remover（VSR）命令封装 —— 擦除视频里的硬字幕 / 水印。

VSR 原生命令行的形式是：
    python backend/main.py -i <输入> -o <输出> -c YMIN YMAX XMIN XMAX --inpaint-mode sttn-auto

本模块替用户处理掉三个坑：

1. **-o 其实是必填的**。`backend/main.py` 里直接 `sr.video_out_path = args.output`，
   而 `--help` 写的是 optional，不传会崩。这里在包装层兜底算出输出路径。

2. **必须在 VSR 根目录下运行**。`backend/config.py` 里配置文件路径是相对路径，
   换个目录跑会读不到配置。这里固定用 cwd=VSR_ROOT。

3. **选区要用绝对像素**，而 GUI 里配的默认值 `0.88,0.99,0.15,0.85` 是比例。
   这里用 ffprobe 读视频宽高后自动换算，复现出与 GUI 完全一致的默认行为。

另外默认只处理底部区域（比全屏自动检测快很多）。素材里有满屏花字时
加 `--full` 就能退回全屏自动检测。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from .. import config, env, media, runner, ui
from . import ensure, output_path_for

# VSR 支持的擦除算法。说明文字摘自它的 README，
# 补上了「在什么情况下该选它」，方便直接照着挑。
INPAINT_MODES: list[tuple[str, str]] = [
    ("sttn-auto", "STTN 智能擦除 — 不检测字幕直接按选区重绘，最快，真人视频效果好（默认）"),
    ("sttn-det", "STTN 带检测 — 先检测字幕位置再重绘，比 auto 慢但更准"),
    ("lama", "LAMA — 对动画类视频效果好，速度一般，不可跳过检测"),
    ("propainter", "ProPainter — 运动剧烈的视频效果好，吃显存、速度慢"),
    ("opencv", "OpenCV — 传统图像修补，最快但效果最粗糙，仅用于试跑"),
]

# 底部选区（比例）：高度 90%-100%、宽度 15%-85%
#
# 相比图形界面的默认值 0.88,0.99,0.15,0.85，只把选区整体下移了一点，
# 让下沿贴住画面底边。原因：字幕贴着底边写时，下沿停在 99% 会剩最后
# 十几个像素擦不掉，成片底部留一条细碎残影（竖屏带货素材很常见）。
#
# 注意别顺手把选区加大 —— 实测把高度扩到 86%-100%、宽度扩到 10%-90% 之后，
# STTN 需要重建的面积变大，反而擦不干净（同一段素材的字还能认出来）。
# 高度维持在 10% 左右、只挪位置，效果与图形界面默认值一致。
DEFAULT_BOTTOM_RATIO = (0.90, 1.00, 0.15, 0.85)

# 顶部区域，给上方带标题/水印的素材用
TOP_RATIO = (0.02, 0.18, 0.05, 0.95)

# 视频 / 图片扩展名，用于判断是否支持
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm", ".m4v", ".ts", ".mpg", ".mpeg",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------


def add_arguments(parser) -> None:
    """给 vct desub 子命令注册参数。"""
    parser.add_argument("input", help="输入视频或图片路径")
    parser.add_argument(
        "-o", "--output",
        help="输出路径（默认：输入文件同目录下的 <文件名>_no_sub.mp4）",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=[mode for mode, _ in INPAINT_MODES],
        default=None,
        help="擦除算法，默认 sttn-auto",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="全屏自动检测所有文本（不指定区域，速度较慢）",
    )
    parser.add_argument(
        "-c", "--area",
        nargs=4, type=int, action="append", metavar=("YMIN", "YMAX", "XMIN", "XMAX"),
        dest="areas",
        help="自定义擦除区域（绝对像素），可重复指定多个区域",
    )
    parser.add_argument(
        "--area-ratio",
        action="append",
        metavar="YMIN,YMAX,XMIN,XMAX",
        help="自定义擦除区域（0-1 的比例，如 0.88,0.99,0.15,0.85），可重复",
    )
    parser.add_argument(
        "--top",
        action="store_true",
        # 注意：argparse 的 help 字符串会做 % 格式化，字面百分号要写成 %%
        help="使用顶部区域（高度 2%%-18%%、宽度 5%%-95%%），适合顶部有标题/水印的素材",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要执行的命令，不真正执行",
    )


def build_options(
    input_path: str,
    output: str | None = None,
    mode: str | None = None,
    full: bool = False,
    top: bool = False,
    areas: list[tuple[int, int, int, int]] | None = None,
    area_ratio: list[str] | None = None,
    dry_run: bool = False,
) -> SimpleNamespace:
    """构造一个与命令行参数同构的参数对象。

    交互式菜单和组合流程用它来复用 run() 的完整逻辑，
    避免把「算输出路径、换算选区」这些规则抄两遍。
    """
    return SimpleNamespace(
        input=input_path,
        output=output,
        mode=mode,
        full=full,
        top=top,
        areas=areas,
        area_ratio=area_ratio,
        dry_run=dry_run,
    )


def run(args) -> int:
    """执行 vct desub。"""
    source = Path(ui.clean_path(args.input)).expanduser().resolve()

    # ---- 输入校验 ----
    if not source.exists():
        ui.error(f"输入文件不存在：{source}")
        return 3
    if source.suffix.lower() not in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS:
        ui.warn(f"扩展名 {source.suffix} 不在已知支持列表里，仍会尝试处理")

    # ---- 环境检查 ----
    if not ensure(env.probe_vsr(), dry_run=args.dry_run):
        return 4

    # ---- 算法 ----
    mode = args.mode or config.get("desub_mode", "sttn-auto")
    if mode not in {m for m, _ in INPAINT_MODES}:
        ui.error(f"未知的擦除算法：{mode}")
        ui.hint(f"可选：{'、'.join(m for m, _ in INPAINT_MODES)}")
        return 2

    # ---- 输出路径兜底（VSR 的 -o 其实是必填的）----
    if args.output:
        output = Path(ui.clean_path(args.output)).expanduser().resolve()
    else:
        is_image = source.suffix.lower() in IMAGE_EXTENSIONS
        suffix = "" if is_image else "_no_sub"
        output = Path(output_path_for(str(source), suffix, source.suffix))
        if is_image:
            # VSR 对图片的处理是存到输入同目录的 no_sub/ 下
            output = source.parent / "no_sub" / source.name

    # ---- 组装擦除区域 ----
    regions, region_desc = _resolve_regions(args, source, args.dry_run)
    if regions is None:
        return 2

    # ---- 拼命令 ----
    # dry-run 时即使环境没装也把命令打出来，方便用户先确认参数
    vsr_cmd, vsr_cwd = env.vsr_command(allow_missing=args.dry_run)
    command = vsr_cmd + [
        "-i", str(source),
        "-o", str(output),
        "--inpaint-mode", mode,
    ]
    for region in regions:
        command += ["-c"] + [str(value) for value in region]

    # ---- 执行前的说明 ----
    ui.blank()
    ui.kv_table([
        ("输入", str(source)),
        ("输出", str(output)),
        ("算法", f"{mode} — {_mode_label(mode)}"),
        ("擦除区域", region_desc),
    ])
    _warn_if_slow(source, mode, regions, args)

    result = runner.run(
        command,
        cwd=vsr_cwd,
        dry_run=args.dry_run,
        tag=f"desub-{mode}",
        title="擦除硬字幕",
    )

    if result.ok and not args.dry_run:
        config.update({
            "desub_mode": mode,
            "last_input": str(source),
            "last_output_dir": str(output.parent),
        })
        config.remember_file(source)
        ui.blank()
        print("  " + ui.bold_text("产出文件："))
        if output.exists():
            ui.ok(ui.describe_file(output))
        else:
            ui.warn(f"未在预期位置找到输出：{output}")

    return result.returncode


def _mode_label(mode: str) -> str:
    """取算法说明里破折号后面的部分。"""
    for value, label in INPAINT_MODES:
        if value == mode:
            return label
    return mode


def _resolve_regions(args, source: Path, dry_run: bool):
    """决定最终传给 VSR 的擦除区域。

    Returns:
        (区域列表, 中文描述)。区域列表为 None 表示参数有误，调用方应中止。
        区域列表为空列表表示全屏自动检测（不传 -c）。
    """
    # 1) 显式指定的绝对像素区域优先
    if args.areas:
        areas = [tuple(item) for item in args.areas]
        return areas, _describe_pixel_areas(areas, args)

    # 2) 显式指定的比例区域
    if args.area_ratio:
        size = media.video_size(source)
        if size is None and not dry_run:
            ui.error("读不出视频宽高，无法换算比例选区")
            ui.hint("请改用 -c 传绝对像素坐标，或确认 ffprobe 可用（vct doctor）")
            return None, ""
        width, height = size if size else (1080, 1920)
        ratios = []
        for text in args.area_ratio:
            parsed = media.parse_ratio(text)
            if parsed is None:
                ui.error(f"比例选区格式不对：{text}")
                ui.hint("正确格式示例：0.88,0.99,0.15,0.85（依次是 ymin,ymax,xmin,xmax，取值 0-1）")
                return None, ""
            ratios.append(parsed)
        areas = [media.ratio_to_pixels(r, width, height) for r in ratios]
        desc = "、".join(f"{a[2]},{a[0]} → {a[3]},{a[1]}" for a in areas)
        return areas, f"比例换算为像素（视频 {width}x{height}）：{desc}"

    # 3) --full：全屏自动检测
    if args.full:
        return [], "全屏自动检测所有文本"

    # 4) --top：顶部区域
    if args.top:
        areas = _ratio_areas(TOP_RATIO, source, dry_run)
        if areas is None:
            return None, ""
        return areas, "顶部区域（高度 2%-18%、宽度 5%-95%）"

    # 5) 默认：底部区域（见 DEFAULT_BOTTOM_RATIO 的说明）
    mode = args.mode or config.get("desub_mode", "sttn-auto")
    if mode.startswith("sttn"):
        # STTN 的 auto 模式不做字幕检测，没有选区就等于整屏重绘，必须先给区域
        areas = _ratio_areas(DEFAULT_BOTTOM_RATIO, source, dry_run)
        if areas is None:
            return None, ""
        return areas, "底部区域（高度 90%-100%、宽度 15%-85%）"

    # 其他算法带字幕检测，不给区域就是全屏检测，行为更稳妥
    return [], "全屏自动检测所有文本（当前算法自带检测）"


def _ratio_areas(ratio, source: Path, dry_run: bool):
    """把一组比例选区换算成像素选区。失败返回 None。"""
    size = media.video_size(source)
    if size is None:
        if dry_run:
            size = (1080, 1920)  # dry-run 时用竖屏常见尺寸占位，只为把命令打出来
        else:
            ui.error("读不出视频宽高，无法推算擦除区域")
            ui.hint("请改用 -c 传绝对像素坐标，或确认 ffprobe 可用（vct doctor）")
            return None
    width, height = size
    return [media.ratio_to_pixels(ratio, width, height)]


def _describe_pixel_areas(areas, args) -> str:
    """给绝对像素选区生成中文描述。"""
    parts = [f"({a[0]},{a[2]}) → ({a[1]},{a[3]})" for a in areas]
    if len(areas) == 1 and tuple(areas[0]) == (0, 0, 0, 0):
        return "全屏"
    return "、".join(parts)


def _warn_if_slow(source: Path, mode: str, regions, args) -> None:
    """对明显会很慢或有风险的组合给出提前提醒。"""
    duration = media.duration_seconds(source)
    if duration and duration > 300 and mode in ("propainter", "lama"):
        ui.warn(
            f"这个视频有 {duration / 60:.1f} 分钟，{mode} 算法较慢，"
            f"预计需要较长时间。急的话可以改用 sttn-auto。"
        )
    if mode == "propainter":
        ui.warn("ProPainter 很吃显存；本机是 Apple 芯片，显存与内存共享，可能触发内存交换。")
    if not regions and mode.startswith("sttn"):
        ui.warn("STTN 不带检测时若没给区域，会对整帧重绘，画面可能被抹掉细节。")


# --------------------------------------------------------------------------
# 交互式
# --------------------------------------------------------------------------


def interactive_desub() -> int:
    """交互式：擦除硬字幕。"""
    ui.section("擦除硬字幕 / 水印")
    ui.hint("首次运行会自动合并模型分片，会多花一点时间。")

    probe = env.probe_vsr()
    if not probe.ok:
        return _guide_missing_env(probe)

    source = ui.ask_path("输入视频或图片", default=config.get("last_input"))

    ui.blank()
    ui.hint("字幕通常在哪？（不确定就选底部）")
    preset = ui.ask_choice(
        "擦除区域",
        [
            ("bottom", "底部 — 高度 88%-99%、宽度 15%-85%，最常见，最快"),
            ("full", "全屏 — 自动找出所有文本（含满屏花字），较慢"),
            ("top", "顶部 — 高度 2%-18%，适合顶部标题/水印"),
            ("custom", "手动输入坐标或比例"),
        ],
        default=config.get("desub_area", "bottom"),
    )

    # 把交互选择转成参数对象，复用命令行模式的同一套逻辑
    args = build_options(
        input_path=source,
        full=preset == "full",
        top=preset == "top",
    )

    if preset == "custom":
        size = media.video_size(source)
        if size:
            width, height = size
            ui.hint(f"该视频尺寸：{width}x{height}")
        else:
            ui.hint("读不出视频尺寸，请直接填绝对像素")
        ui.hint("可以填比例（如 0.88,0.99,0.15,0.85）或绝对像素（如 950 1080 160 920）")
        raw = ui.ask("区域坐标")
        parsed_pixels = _try_parse_pixels(raw)
        if parsed_pixels:
            args.areas = [parsed_pixels]
        else:
            args.area_ratio = [raw]

    mode = ui.ask_choice("擦除算法", INPAINT_MODES, default=config.get("desub_mode", "sttn-auto"))

    default_out = output_path_for(source, "_no_sub", Path(source).suffix)
    ui.hint("直接回车用默认路径")
    output = ui.ask_path("输出文件", default=default_out, must_exist=False)
    args.output = output
    args.mode = mode

    code = run(args)
    if code == 0:
        config.update({"desub_area": preset, "desub_mode": mode})
    return code


def _try_parse_pixels(text: str) -> tuple[int, int, int, int] | None:
    """尝试把一段文本解析成 4 个整数（绝对像素选区）。失败返回 None。"""
    import re

    parts = re.split(r"[,;\s]+", text.strip())
    if len(parts) != 4:
        return None
    try:
        return tuple(int(p) for p in parts)  # type: ignore[return-value]
    except ValueError:
        return None


def _guide_missing_env(probe: env.Probe) -> int:
    """VSR 环境缺失时，给出安装引导。"""
    from . import setup_vsr

    ui.error("还差一步：video-subtitle-remover 的运行环境没装")
    ui.blank()
    ui.hint(probe.detail)
    ui.blank()
    print("  " + ui.bold_text("源码和模型都齐了，只差 Python 依赖。"))
    if ui.ask_yes_no("现在要看看安装方法吗？", default=True):
        setup_vsr.explain()
    return 4
