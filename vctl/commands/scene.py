"""
智能镜头分割：把一条多镜头视频按画面自动拆成若干片段。

这是「剪映 → 智能镜头分割」那个功能的命令行版本。用途是拿到别人的产品
视频（`*_raw.mp4` 这类几十秒到几分钟、平均两三个镜头一切换的素材）之后，
自动拆成单镜头片段，方便重新挑选、拼接、二次剪辑。

    输入:  多镜头视频.mp4（29 个镜头）

    第 1 步  检测画面跳变  →  镜头切点清单      （默认只到这一步）
    第 2 步  按切点切割    →  clip_001.mp4 ...  （加 --split）

检测由 PySceneDetect 完成，它是个独立 CLI 工具，vct 只负责调用它 —— 与
ffmpeg 的处理方式一致，不装进 vct 自己的解释器，也不动 VideoCaptioner 的
虚拟环境。

为什么用 PySceneDetect 而不是 ffmpeg 自带的 scdet？两者都基于帧间差异，但
scdet 只有一个裸阈值、没有最短片段保护，实测阈值从 5 调到 10 切点数会从 8
个塌到 2 个，快剪素材很容易切成碎片或漏切。PySceneDetect 的 AdaptiveDetector
对阈值做滚动平均，能压住运镜和闪光造成的误判。

切割默认**重新编码**，这是被实测逼出来的：

    这批素材的关键帧间隔约 7 秒（0 / 7.033 / 14.033 / 21.033 …），而平均镜头
    只有 4 秒。`-c copy` 不重新编码，片段起点只能落在关键帧上 —— 于是
    scene 2/3/4 的片段全都从第 0 秒开始，请求 1.6 秒的片段实得 3.4 秒，
    越往后越长（29 个片段合计 214 秒，原片才 115 秒）。

所以 `-c copy` 在这里不是「稍差一点」，而是**根本切不对**，只适合关键帧
密集（比如每秒都有）的片源。`--copy` 保留下来给那种场景，默认走重编码。

注意这里**不涉及大模型**：镜头边界是纯视觉信号（相邻帧的直方图差分），
大模型看不见画面，抽帧喂多模态的精度远不如直方图法，成本却高一个数量级。
"""

from __future__ import annotations

import csv
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .. import env, media, ui
from .. import runner as runner_mod
from . import ensure

# 检测器：命令名 → (中文说明, 适用场景)
DETECTORS: dict[str, tuple[str, str]] = {
    "adaptive": ("自适应", "抗运镜、抗闪光，默认"),
    "content": ("内容", "标准快切，对画面变化更敏感"),
    "threshold": ("阈值", "淡入淡出、切黑场"),
}

# 重编码参数。crf 18 是视觉上几乎无损的档位 —— 切片段是中间步骤，后面还要
# 再剪再压，这里不能再掉一次画质。
VIDEO_CRF = "18"
VIDEO_PRESET = "medium"
AUDIO_BITRATE = "192k"


# --------------------------------------------------------------------------
# 数据结构与格式化
# --------------------------------------------------------------------------


@dataclass
class Scene:
    """一个镜头片段。时间单位统一用秒（float），与 PySceneDetect 的 CSV 一致。"""

    number: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def _format_timecode(seconds: float) -> str:
    """把秒数格式化成 HH:MM:SS.mmm，与 PySceneDetect 的输出风格保持一致。"""
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _display_width(text: str) -> int:
    """算字符串在终端里占多少列。中文等全角字符占两列，否则表格会歪。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补空格（不是按字符数），让含中文的表头也能对齐。"""
    padding = " " * max(0, width - _display_width(text))
    return padding + text if align == "right" else text + padding


# --------------------------------------------------------------------------
# 检测
# --------------------------------------------------------------------------


def _build_detect_command(
    scenedetect: str,
    source: Path,
    work_dir: Path,
    detector: str,
    threshold: float | None,
    min_len: float,
) -> list[str]:
    """拼出 scenedetect 的检测命令。

    参数顺序有讲究：全局选项（-i/-o/-q/-m）必须放在检测子命令**之前**，
    这是 PySceneDetect 的硬性要求。

    `-m` 的值一定要带 `s` 后缀 —— 不带会被当成**帧数**而不是秒数
    （`-m 1` 是 1 帧，`-m 1s` 才是 1 秒）。
    """
    command = [
        scenedetect,
        "-i", str(source),
        "-o", str(work_dir),
        # 静默：进度条走的是 stderr 且用 \r 刷新，留在输出里会糊成一条长行。
        # 检测耗时靠 ui.step 里的说明给预期。
        "-q",
        "-m", f"{min_len}s",
        f"detect-{detector}",
    ]
    # 不传 threshold 时交给 PySceneDetect 用它自己的默认值，
    # 我们不去复制一份可能随版本变化的默认表。
    if threshold is not None:
        command += ["--threshold", str(threshold)]
    # --skip-cuts 让 CSV 去掉首行的切割清单，成为标准 RFC 4180，好解析
    command += ["list-scenes", "--skip-cuts"]
    return command


def _parse_scenes_csv(csv_path: Path) -> list[Scene]:
    """解析 PySceneDetect 写的场景 CSV。

    表头形如：
        Scene Number,Start Frame,Start Timecode,Start Time (seconds),...
    我们只取序号和起止秒数 —— 时间码和帧号 CSV 里也有，但那两份数据是
    给别的工具用的，这里用不上。
    """
    scenes: list[Scene] = []
    # utf-8-sig：兼容带 BOM 的情况，不带 BOM 时它等同于 utf-8
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                scenes.append(
                    Scene(
                        number=int(row["Scene Number"]),
                        start=float(row["Start Time (seconds)"]),
                        end=float(row["End Time (seconds)"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"场景 CSV 第 {row!r} 行无法解析：{exc}") from exc
    return scenes


def _find_scenes_csv(work_dir: Path) -> Path | None:
    """在临时目录里找 PySceneDetect 写出的 CSV。

    默认文件名是 `$VIDEO_NAME-Scenes.csv`，视频名里可能带空格或中文，
    与其拼名字不如直接找 —— 这个目录是我们自己建的空目录，不会有别的 CSV。
    """
    candidates = sorted(work_dir.glob("*.csv"))
    return candidates[0] if candidates else None


def _detect(
    source: Path,
    detector: str,
    threshold: float | None,
    min_len: float,
    dry_run: bool,
) -> list[Scene] | None:
    """跑一次镜头检测，返回镜头列表；失败返回 None。"""
    scenedetect = env.probe_scenedetect().path
    if scenedetect is None:
        return None

    with tempfile.TemporaryDirectory(prefix="vct-scene-") as tmp:
        work_dir = Path(tmp)
        command = _build_detect_command(
            str(scenedetect), source, work_dir, detector, threshold, min_len
        )
        # 不传 title：外面已经用 ui.step 报过「[1/2] 检测镜头切换」了，
        # runner 再报一次就成了两行重复的标题。
        result = runner_mod.run(
            command,
            tag="scene-detect",
            dry_run=dry_run,
        )
        if dry_run:
            # dry-run 下没有真实产出，返回空列表表示「到此为止」
            return []
        if not result.ok:
            ui.error(f"镜头检测失败（退出码 {result.returncode}）")
            return None

        csv_path = _find_scenes_csv(work_dir)
        if csv_path is None:
            ui.error("镜头检测没有产出场景清单，无法继续")
            return None
        try:
            return _parse_scenes_csv(csv_path)
        except (OSError, ValueError) as exc:
            ui.error(f"解析场景清单失败：{exc}")
            return None


# --------------------------------------------------------------------------
# 展示
# --------------------------------------------------------------------------


def _print_scene_table(scenes: list[Scene], detector: str, threshold: float | None, min_len: float) -> None:
    """打印镜头切点表格。"""
    label, _ = DETECTORS[detector]
    options = [f"检测器 {label}", f"最短镜头 {min_len}s"]
    if threshold is not None:
        options.append(f"阈值 {threshold}")

    ui.section(f"镜头切点（{'，'.join(options)}）")

    headers = ["#", "开始", "结束", "时长"]
    rows = [
        (
            str(scene.number),
            _format_timecode(scene.start),
            _format_timecode(scene.end),
            f"{scene.duration:.2f}s",
        )
        for scene in scenes
    ]

    # 按显示宽度算列宽，中文表头才不会把表格撑歪
    widths = [
        max(_display_width(headers[i]), max((_display_width(r[i]) for r in rows), default=0))
        for i in range(4)
    ]
    aligns = ["right", "left", "left", "right"]

    line = "  " + "  ".join(
        _pad(headers[i], widths[i], aligns[i]) for i in range(4)
    )
    print(ui.bold_text(line))
    print("  " + ui.dim_text("─" * (sum(widths) + 6)))
    for row in rows:
        print(
            "  "
            + "  ".join(_pad(row[i], widths[i], aligns[i]) for i in range(4))
        )


def _print_summary(scenes: list[Scene]) -> None:
    """打印统计信息。"""
    durations = [scene.duration for scene in scenes]
    ui.blank()
    ui.kv_table(
        [
            ("镜头数", f"{len(scenes)} 个"),
            ("平均时长", f"{sum(durations) / len(durations):.2f} 秒"),
            ("最短 / 最长", f"{min(durations):.2f} 秒 / {max(durations):.2f} 秒"),
        ],
        key_width=14,
    )


# --------------------------------------------------------------------------
# 切割
# --------------------------------------------------------------------------


def _build_split_command(
    ffmpeg: str, source: Path, scene: Scene, target: Path, copy_mode: bool
) -> list[str]:
    """拼出切一个片段的 ffmpeg 命令。

    `-ss` 放在 `-i` 之前是**输入定位**：ffmpeg 直接跳到目标位置附近，
    不用从头解码，慢片源上能快很多。重编码下它依然是精确的 —— ffmpeg 会
    从关键帧开始解码，然后丢掉目标点之前的帧。

    两种模式：
      重编码（默认）—— 起点精确落在切点上，代价是慢、且多一代画质损失
      --copy       —— `-c copy` 不重新编码，快且无损，但起点只能落在关键帧上

    为什么 copy 不能当默认：见模块开头的说明。这批素材关键帧间隔约 7 秒，
    而镜头平均 4 秒一个，copy 出来的片段会带上整段前置画面。
    """
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", f"{scene.start:.3f}",
        "-i", str(source),
        "-t", f"{scene.duration:.3f}",
    ]
    if copy_mode:
        # -avoid_negative_ts 让复制模式下产出的片段从 0 开始计时，
        # 否则前面切掉的那一段会留下负时间戳，某些播放器会花屏或没声音
        command += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    else:
        command += [
            "-c:v", "libx264",
            "-crf", VIDEO_CRF,
            "-preset", VIDEO_PRESET,
            "-pix_fmt", "yuv420p",   # 兼容老播放器 / 剪辑软件
            "-c:a", "aac",
            "-b:a", AUDIO_BITRATE,
        ]
    command.append(str(target))
    return command


def _split_scenes(
    source: Path, scenes: list[Scene], out_dir: Path, copy_mode: bool, dry_run: bool
) -> tuple[int, int]:
    """按切点逐段切割。返回 (成功数, 失败数)。"""
    ffmpeg = media.find_tool("ffmpeg")
    if ffmpeg is None:
        ui.error("找不到 ffmpeg，无法切割")
        ui.hint("把工具箱自带的 ffmpeg 加进 PATH：")
        ui.hint(f'    export PATH="{env.FFMPEG_BIN_DIR}:$PATH"')
        return 0, len(scenes)

    succeeded = 0
    failed = 0
    total = len(scenes)
    for index, scene in enumerate(scenes, start=1):
        target = out_dir / f"{source.stem}_clip_{scene.number:03d}{source.suffix}"
        command = _build_split_command(ffmpeg, source, scene, target, copy_mode)
        # quiet：几十个片段的 ffmpeg 命令行长得几乎一样，逐条回显会把
        # 真正要看的进度和结果冲掉。日志照写，失败时依然给得出路径。
        result = runner_mod.run(
            command,
            tag="scene-split",
            dry_run=dry_run,
            quiet=True,
        )
        if result.ok:
            succeeded += 1
            ui.ok(f"[{index}/{total}] {target.name}  {scene.duration:.2f}s")
        else:
            failed += 1
            ui.error(f"[{index}/{total}] {target.name} 失败（退出码 {result.returncode}）")
    return succeeded, failed


def _export_csv(scenes: list[Scene], target: Path) -> bool:
    """把切点清单导成 CSV。"""
    try:
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["序号", "开始(秒)", "结束(秒)", "时长(秒)", "开始时间码", "结束时间码"])
            for scene in scenes:
                writer.writerow(
                    [
                        scene.number,
                        f"{scene.start:.3f}",
                        f"{scene.end:.3f}",
                        f"{scene.duration:.3f}",
                        _format_timecode(scene.start),
                        _format_timecode(scene.end),
                    ]
                )
    except OSError as exc:
        ui.error(f"导出 CSV 失败：{exc}")
        return False
    return True


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------


def add_arguments(parser) -> None:
    """给 vct scene 子命令注册参数。"""
    parser.add_argument("input", help="输入视频路径")
    parser.add_argument(
        "--split",
        action="store_true",
        help="按检测到的切点切出片段文件（默认只打印切点清单）",
    )
    parser.add_argument(
        "-o", "--output-dir",
        help="输出目录（默认 <视频同目录>/<文件名>_scenes/）",
    )
    parser.add_argument(
        "--detector",
        choices=list(DETECTORS),
        default="adaptive",
        help="检测算法，默认 adaptive（抗运镜、抗闪光）",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="检测阈值，越小越敏感（不传则用 PySceneDetect 的默认值：adaptive 3.0 / content 27.0 / threshold 12.0）",
    )
    parser.add_argument(
        "--min-len",
        type=float,
        default=0.6,
        help="最短镜头秒数，短于此的会被并进相邻镜头，默认 0.6",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="切割时直接复制流，不重新编码（快且无损，但片段起点只能落在关键帧上，"
             "关键帧稀疏的片源会带上整段前置画面）",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="额外把切点清单导出成 CSV",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要执行的命令，不真正执行",
    )


def run(args) -> int:
    """执行 vct scene。"""
    source = Path(ui.clean_path(args.input)).expanduser().resolve()

    if not source.exists():
        ui.error(f"输入文件不存在：{source}")
        return 3
    if not source.is_file():
        ui.error(f"输入不是文件：{source}")
        return 3

    if args.min_len <= 0:
        ui.error("--min-len 必须大于 0")
        return 2

    probe = env.probe_scenedetect()
    if not ensure(probe, dry_run=args.dry_run):
        return 4

    # ---- 输出目录（只在真正要写文件时才建）----
    needs_output = args.split or args.csv
    if args.output_dir:
        out_dir = Path(ui.clean_path(args.output_dir)).expanduser().resolve()
    else:
        out_dir = source.parent / f"{source.stem}_scenes"

    ui.header(
        "智能镜头分割",
        f"输入：{source.name}",
    )
    label, note = DETECTORS[args.detector]
    threshold_text = str(args.threshold) if args.threshold is not None else "默认"
    ui.kv_table(
        [
            ("检测器", f"{label}（{note}）"),
            ("阈值", threshold_text),
            ("最短镜头", f"{args.min_len} 秒"),
            ("切割方式", "直接复制（快，起点吸附关键帧）" if args.copy else "重编码（精确）"),
            ("输出", str(out_dir) if needs_output else "只列清单，不写文件"),
        ]
    )
    if args.copy and args.split:
        ui.warn("--copy 不重新编码，片段起点只能落在关键帧上。")
        ui.hint("源文件关键帧间隔偏大的话（常见 2-10 秒），切出的片段会带上前一镜头的尾巴。")
        ui.hint("要精确的切点就去掉 --copy，代价是重新编码一遍。")

    # ======================================================================
    # 第 1 步：检测
    # ======================================================================
    ui.step("[1/2] 检测镜头切换")
    ui.hint("视视频长度而定，一两分钟的素材通常十来秒。")
    scenes = _detect(
        source, args.detector, args.threshold, args.min_len, args.dry_run
    )

    if args.dry_run:
        ui.blank()
        ui.info("dry-run 到此为止。真正执行时，上面检测出的每个切点会各自生成一条 ffmpeg 切割命令。")
        return 0

    if scenes is None:
        return 5

    if not scenes:
        # PySceneDetect 对一条正常视频至少会报一个场景，所以「一行都没有」
        # 不是「没有切点」，而是它没写出可用的结果。当成运行时失败。
        ui.error("没能从检测结果里读出任何镜头。")
        return 5

    _print_scene_table(scenes, args.detector, args.threshold, args.min_len)
    _print_summary(scenes)

    # 只有一个「场景」= 全片没有画面跳变。这是个合法结果，但光看表格里那行
    # 「镜头数 1 个」不容易意识到，而且此时切文件等于把原片复制一份 ——
    # 明确说清楚，别让用户白等一遍重编码。
    single_shot = len(scenes) == 1
    if single_shot:
        ui.blank()
        ui.warn("整条视频只有一个镜头 —— 没有检测到任何画面跳变。")
        ui.hint("如果这与实际不符，换个检测器或调低阈值试试：")
        ui.hint(f"    vct scene {ui.dim_text(str(source))} --detector content --threshold 20")

    # ======================================================================
    # 第 2 步：切割 / 导出
    # ======================================================================
    # 单镜头时切出来的东西和原片一模一样，白跑一遍重编码没有意义。
    do_split = args.split and not single_shot

    # 目录只在真要往里写东西时才建 —— 单镜头 + --split 是「什么都不写」，
    # 那就别留下一个空目录让人以为切失败了。
    if do_split or args.csv:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            ui.error(f"创建输出目录失败：{exc}")
            return 5

    succeeded = 0
    failed = 0

    if do_split:
        ui.step(f"[2/2] 切割 {len(scenes)} 个片段")
        succeeded, failed = _split_scenes(
            source, scenes, out_dir, args.copy, args.dry_run
        )
    elif args.csv:
        ui.step("[2/2] 导出切点清单")

    if args.csv:
        csv_target = out_dir / f"{source.stem}_scenes.csv"
        if _export_csv(scenes, csv_target):
            ui.ok(f"切点清单 → {csv_target}")

    # ======================================================================
    # 汇总
    # ======================================================================
    ui.blank()
    if args.split and single_shot:
        ui.info("没有切点可切，已跳过切割。")
    elif do_split:
        if failed:
            ui.error(f"切割完成：成功 {succeeded} 个，失败 {failed} 个")
            return 5
        ui.ok(f"切割完成：{succeeded} 个片段 → {out_dir}")
    elif not args.csv:
        ui.hint("这只是清单。要真的切出文件，加 --split：")
        ui.hint(f"    vct scene {ui.dim_text(str(source))} --split")

    return 0


# --------------------------------------------------------------------------
# 交互式
# --------------------------------------------------------------------------


class SimpleArgs:
    """给交互式流程用的参数容器，字段名与 argparse 的命名空间保持一致。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def interactive_scene() -> int:
    """交互式：智能镜头分割。"""
    from .. import config

    ui.section("智能镜头分割")
    ui.hint("把多镜头视频按画面自动拆成单镜头片段 —— 剪映「智能镜头分割」的命令行版。")

    if not env.probe_scenedetect().ok:
        probe = env.probe_scenedetect()
        ui.warn(probe.detail)
        ui.hint(probe.fix)
        ui.pause()
        return 4

    source = ui.ask_path("输入视频", default=config.get("last_input"))

    what = ui.ask_choice(
        "做到哪一步",
        [
            ("list", "只看切点清单（不写文件）"),
            ("split", "切出片段文件"),
        ],
        default="list",
    )

    ui.blank()
    detector = ui.ask_choice(
        "检测算法",
        [
            ("adaptive", "自适应 — 抗运镜、抗闪光（推荐）"),
            ("content", "内容 — 标准快切，更敏感"),
            ("threshold", "阈值 — 淡入淡出、切黑场"),
        ],
        default="adaptive",
    )

    # 默认值交给 PySceneDetect，让用户按回车即可
    default_min_len = str(config.get("scene_min_len", 0.6))
    min_len_text = ui.ask("最短镜头秒数", default=default_min_len)
    try:
        min_len = float(min_len_text)
        if min_len <= 0:
            raise ValueError
    except ValueError:
        ui.warn(f"「{min_len_text}」不是有效秒数，改用默认值 0.6")
        min_len = 0.6

    copy_mode = False
    output_dir = None
    if what == "split":
        ui.blank()
        copy_mode = ui.ask_yes_no(
            "切割时直接复制流（不重新编码）？\n"
            "      （默认重新编码：慢一些，但切点精确。\n"
            "        复制虽然快且无损，可起点只能落在关键帧上，\n"
            "        关键帧间隔大时会带上前一镜头的尾巴）",
            default=False,
        )
        output_dir = ui.ask_path(
            "输出目录",
            default=str(Path(source).parent / f"{Path(source).stem}_scenes"),
            must_exist=False,
            kind="dir",
        )

    config.set("scene_min_len", min_len)

    args = SimpleArgs(
        input=source,
        split=what == "split",
        output_dir=output_dir,
        detector=detector,
        threshold=None,
        min_len=min_len,
        copy=copy_mode,
        csv=False,
        dry_run=False,
    )
    return run(args)
