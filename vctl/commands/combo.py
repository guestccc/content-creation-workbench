"""
组合流程：提取字幕 + 擦除字幕。

这是带货素材二次创作里最常用的两个动作，一条命令一次做完：

    输入:  别人的视频（画面里有硬字幕）

    步骤 1  语音转字幕  →  <文件名>.srt          （拿到文案，可改写复用）
    步骤 2  擦除硬字幕  →  <文件名>_no_sub.mp4   （拿到干净画面，可重新加字幕）

两步互相独立：第 1 步失败不会挡住第 2 步。想只做其中一半，用
`--no-extract` / `--no-desub` 即可。

为什么要先转录再擦除？转录快、擦除慢，先做快的能更早拿到文案；
而且转录读的是原始音轨，跟画面有没有字幕无关。
"""

from __future__ import annotations

import time
from pathlib import Path

from .. import config, env, ui
from . import output_path_for
from . import vc as vc_cmd
from . import vsr as vsr_cmd


def add_arguments(parser) -> None:
    """给 vct extract-desub 子命令注册参数。"""
    parser.add_argument("input", help="输入视频路径")
    parser.add_argument(
        "--asr",
        default=None,
        choices=["bijian", "jianying", "whisper-api", "whisper-cpp"],
        help="语音识别引擎，默认的 bijian 免费且无需配置",
    )
    parser.add_argument(
        "--no-extract",
        action="store_true",
        help="跳过「提取字幕」，只擦除字幕",
    )
    parser.add_argument(
        "--no-desub",
        action="store_true",
        help="跳过「擦除字幕」，只提取字幕",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="擦除时全屏自动检测（适合满屏花字），默认只处理底部区域",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=[mode for mode, _ in vsr_cmd.INPAINT_MODES],
        default=None,
        help="擦除算法，默认 sttn-auto",
    )
    parser.add_argument(
        "-o", "--output-dir",
        help="产出的输出目录（默认与输入文件同目录）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要执行的命令，不真正执行",
    )


def run(args) -> int:
    """执行 vct extract-desub。"""
    source = Path(ui.clean_path(args.input)).expanduser().resolve()

    if not source.exists():
        ui.error(f"输入文件不存在：{source}")
        return 3

    do_extract = not args.no_extract
    do_desub = not args.no_desub
    if not do_extract and not do_desub:
        ui.error("--no-extract 和 --no-desub 不能同时指定，那就什么都不做了")
        return 2

    # ---- 输出目录 ----
    if args.output_dir:
        out_dir = Path(ui.clean_path(args.output_dir)).expanduser().resolve()
    else:
        out_dir = source.parent
    if not args.dry_run:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            ui.error(f"创建输出目录失败：{exc}")
            return 1

    # 只取文件名，输出目录由 --output-dir 决定
    srt_path = out_dir / f"{source.stem}.srt"
    clean_video = out_dir / Path(output_path_for(str(source), "_no_sub", source.suffix)).name

    ui.header(
        "提取字幕 + 擦除字幕",
        f"输入：{source.name}",
    )
    plan = []
    if do_extract:
        plan.append(f"提取字幕 → {srt_path.name}")
    if do_desub:
        plan.append(f"擦除字幕 → {clean_video.name}")
    ui.kv_table([("计划", "；".join(plan)), ("输出目录", str(out_dir))])

    started = time.time()
    results: list[tuple[str, bool, str]] = []

    # ======================================================================
    # 第 1 步：提取字幕（ASR，快）
    # ======================================================================
    if do_extract:
        engine = args.asr or config.get("asr_engine", "bijian")
        ui.step(f"[1/{int(do_extract) + int(do_desub)}] 提取字幕（{engine}）")

        if not env.probe_ffmpeg().ok and not args.dry_run:
            ui.error("缺少 ffmpeg，无法提取音频做识别")
            ui.hint(env.probe_ffmpeg().fix)
            results.append(("提取字幕", False, "缺少 ffmpeg"))
        else:
            code = vc_cmd.run_vc(
                ["transcribe", str(source), "--asr", engine, "-o", str(srt_path)],
                dry_run=args.dry_run,
            )
            if code == 0:
                detail = ui.describe_file(srt_path) if srt_path.exists() else str(srt_path)
                results.append(("提取字幕", True, detail))
            else:
                results.append(("提取字幕", False, f"退出码 {code}"))
                ui.warn("提取字幕失败，继续执行擦除步骤")

    # ======================================================================
    # 第 2 步：擦除硬字幕（慢）
    # ======================================================================
    if do_desub:
        step_no = 2 if do_extract else 1
        total = int(do_extract) + int(do_desub)
        ui.step(f"[{step_no}/{total}] 擦除硬字幕")

        desub_args = vsr_cmd.build_options(
            input_path=str(source),
            output=str(clean_video),
            mode=args.mode,
            full=args.full,
            dry_run=args.dry_run,
        )
        code = vsr_cmd.run(desub_args)
        if code == 0:
            detail = ui.describe_file(clean_video) if clean_video.exists() else str(clean_video)
            results.append(("擦除字幕", True, detail))
        else:
            results.append(("擦除字幕", False, f"退出码 {code}"))

    # ======================================================================
    # 汇总
    # ======================================================================
    elapsed = time.time() - started
    ui.blank()
    ui.rule("═")
    print("  " + ui.bold_text("处理结果"))
    ui.rule("═")

    for name, succeeded, detail in results:
        if succeeded:
            ui.ok(f"{name}：{detail}")
        else:
            ui.error(f"{name}失败：{detail}")

    ui.blank()
    ui.info(f"总耗时 {elapsed:.1f} 秒")

    if not args.dry_run:
        config.remember_file(source)

    failed = [name for name, succeeded, _ in results if not succeeded]
    if failed:
        ui.blank()
        ui.hint("失败的步骤可以单独重跑：")
        if "提取字幕" in failed:
            ui.hint(f"    vct transcribe {ui.dim_text(str(source))} -o {ui.dim_text(str(srt_path))}")
        if "擦除字幕" in failed:
            ui.hint(f"    vct desub {ui.dim_text(str(source))} -o {ui.dim_text(str(clean_video))}")
        return 5

    # 下一步提示
    ui.blank()
    ui.hint("接下来可以：")
    if do_extract:
        ui.hint(f"    vct subtitle {ui.dim_text(str(srt_path))} \\")
        ui.hint("        --translator bing --target-language zh-Hans   # 翻译/润色文案")
    if do_desub and do_extract:
        ui.hint(f"    vct synthesize {ui.dim_text(str(clean_video))} -s {ui.dim_text(str(srt_path))} \\")
        ui.hint("        --subtitle-mode hard --style anime            # 给干净画面加自己的字幕")

    return 0


# --------------------------------------------------------------------------
# 交互式
# --------------------------------------------------------------------------


def interactive_extract_desub() -> int:
    """交互式：提取字幕 + 擦除字幕。"""
    ui.section("提取字幕 + 擦除字幕")
    ui.hint("一次拿到两样东西：原始文案（srt）+ 干净画面（无字幕视频）。")

    # 两个工具的环境都检查一遍，有问题当场说清楚
    problems = []
    if not env.probe_videocaptioner().ok:
        problems.append(env.probe_videocaptioner())
    if not env.probe_vsr().ok:
        problems.append(env.probe_vsr())

    source = ui.ask_path("输入视频", default=config.get("last_input"))

    ui.blank()
    which = ui.ask_choice(
        "要做哪些步骤",
        [
            ("both", "两样都做 — 提取字幕 + 擦除字幕（推荐）"),
            ("extract", "只提取字幕（口语转文字，带时间轴）"),
            ("desub", "只擦除字幕（画面去字，快）"),
        ],
        default="both",
    )

    if problems and which in ("both", "desub") and any(
        p.name == "video-subtitle-remover" for p in problems
    ):
        from . import setup_vsr

        ui.warn("擦除字幕需要 video-subtitle-remover 的运行环境，目前还没装好。")
        ui.hint(problems[-1].detail)
        if ui.ask_yes_no("现在查看安装方法？", default=True):
            setup_vsr.explain()
            ui.pause()
        return 4

    engine = "bijian"
    if which in ("both", "extract"):
        engine = ui.ask_choice(
            "语音识别引擎",
            [
                ("bijian", "必剪 — 免费，中英文，推荐"),
                ("jianying", "剪映 — 免费，中英文"),
                ("whisper-api", "Whisper API — 需 Key，多语种"),
            ],
            default=config.get("asr_engine", "bijian"),
        )

    full = False
    if which in ("both", "desub"):
        ui.blank()
        ui.hint("画面里的字通常在哪？")
        area = ui.ask_choice(
            "擦除区域",
            [
                ("bottom", "底部字幕 — 最快，最常见"),
                ("full", "全屏 — 含满屏花字，较慢"),
                ("top", "顶部 — 标题或水印"),
            ],
            default="bottom",
        )
        full = area == "full"

    output_dir = ui.ask_path(
        "输出目录",
        default=str(Path(source).parent),
        must_exist=False,
        kind="dir",
    )

    # 复用命令行模式的逻辑
    args = SimpleArgs(
        input=source,
        asr=engine,
        no_extract=which == "desub",
        no_desub=which == "extract",
        full=full,
        mode=None,
        output_dir=output_dir,
        dry_run=False,
    )

    return run(args)


class SimpleArgs:
    """给交互式流程用的参数容器，字段名与 argparse 的命名空间保持一致。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
