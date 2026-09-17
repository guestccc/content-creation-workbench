"""
VideoCaptioner 命令封装。

封装策略：**参数原样透传，不复制上游的参数定义**。
用户敲 `vct transcribe video.mp4 --asr bijian`，实际执行的是
    <VideoCaptioner>/.venv/bin/python -m videocaptioner.cli.main transcribe video.mp4 --asr bijian

这样做的好处：上游升级新增参数时 vct 自动支持，不会因为参数表过时而报错；
`vct transcribe --help` 也能原样透出上游的帮助。

本模块额外负责的只有两件事：
1. 执行前的环境检查（解释器是否存在、ffmpeg 是否可用）
2. 交互式菜单里用到的参数收集函数（interactive_*）
"""

from __future__ import annotations

from pathlib import Path

from .. import config, env, runner, ui
from . import ensure

# 这些子命令依赖 ffmpeg（提取音轨、合成视频、下载视频都要用）
NEEDS_FFMPEG = {"transcribe", "synthesize", "process", "download", "dub"}

# 这些子命令不需要 ffmpeg
NO_FFMPEG = {"style", "config", "gui"}

# 所有可透传的子命令
PASSTHROUGH_COMMANDS = [
    "transcribe",
    "subtitle",
    "synthesize",
    "dub",
    "process",
    "download",
    "style",
    "config",
]


# --------------------------------------------------------------------------
# 核心执行
# --------------------------------------------------------------------------


def run_vc(argv: list[str], dry_run: bool = False) -> int:
    """把参数原样透传给 VideoCaptioner CLI。

    Args:
        argv: 子命令及其参数，例如 ['transcribe', 'video.mp4', '--asr', 'bijian']。
        dry_run: 只回显命令不执行。
    Returns:
        进程退出码。
    """
    if not argv:
        ui.error("没有指定 VideoCaptioner 子命令")
        return 2

    # ---- 环境检查 ----
    probe = env.probe_videocaptioner()
    if not ensure(probe, dry_run=dry_run):
        return 4

    subcommand = argv[0]
    if subcommand in NEEDS_FFMPEG:
        ffmpeg_probe = env.probe_ffmpeg()
        if not ensure(ffmpeg_probe, dry_run=dry_run):
            return 4

    # ---- 拼命令并执行 ----
    try:
        command = env.videocaptioner_command() + argv
    except RuntimeError as exc:
        ui.error(str(exc))
        return 4

    result = runner.run(command, dry_run=dry_run, tag=f"vc-{subcommand}")
    return result.returncode


def launch_gui() -> int:
    """启动 VideoCaptioner 的图形界面。

    GUI 有自己的窗口，交给 run_interactive 让子进程直接接管终端。
    """
    probe = env.probe_videocaptioner()
    if not ensure(probe):
        return 4

    # venv 模式：直接跑 videocaptioner-gui；PATH 命令模式：跑 videocaptioner gui
    if probe.extra.get("mode") == "command":
        command = [str(probe.path), "gui"]
    else:
        gui_binary = Path(probe.path).parent / "videocaptioner-gui"
        if gui_binary.exists():
            command = [str(gui_binary)]
        else:
            # 没装 GUI 入口时回退到子命令形式
            command = [str(probe.path), "-m", "videocaptioner.cli.main", "gui"]

    ui.info("正在启动 VideoCaptioner 图形界面……")
    ui.hint("窗口会在几秒后出现；关掉窗口即退出本命令。")
    result = runner.run_interactive(command, tag="vc-gui")
    return result.returncode


# --------------------------------------------------------------------------
# 交互式参数收集
#
# 每个函数都返回进程退出码（0 成功），由 menu.py 调用。
# 约定：先问参数 → 回显完整命令让用户确认 → 执行。
# --------------------------------------------------------------------------


def _ask_output(
    prompt: str,
    default_path: str,
    description: str = "直接回车用默认路径",
) -> str:
    """询问输出路径（允许文件尚不存在）。"""
    ui.hint(description)
    return ui.ask_path(prompt, default=default_path, must_exist=False)


def interactive_transcribe() -> int:
    """交互式：语音转字幕。"""
    ui.section("语音转字幕（ASR）")
    ui.hint("免费引擎（必剪/剪映）无需任何 API Key，支持中文和英文。")

    source = ui.ask_path("输入音视频文件", default=config.get("last_input"))

    engine = ui.ask_choice(
        "选择识别引擎",
        [
            ("bijian", "必剪 — 免费，中英文，推荐"),
            ("jianying", "剪映 — 免费，中英文"),
            ("whisper-api", "Whisper API — 需 API Key，支持多语种"),
            ("whisper-cpp", "whisper.cpp — 本地模型，支持多语种"),
        ],
        default=config.get("asr_engine", "bijian"),
    )

    default_language = "auto" if engine in ("bijian", "jianying") else config.get("asr_language", "auto")
    language = ui.ask(
        "源语言（zh/en/ja… 或 auto）",
        default=default_language,
    )

    default_out = str(Path(source).with_suffix(".srt"))
    output = _ask_output("输出字幕文件", default_out)

    argv = [source, "--asr", engine, "--language", language, "-o", output]

    # 只有走 API 的引擎才需要额外参数
    if engine == "whisper-api":
        api_key = ui.ask("Whisper API Key", default="", allow_empty=True)
        if api_key:
            argv += ["--whisper-api-key", api_key]
        api_base = ui.ask("Whisper API 地址", default="", allow_empty=True)
        if api_base:
            argv += ["--whisper-api-base", api_base]

    command_line = f"vct transcribe {' '.join(_quote(a) for a in argv)}"
    if not ui.confirm_command(command_line):
        return 0

    config.update({"asr_engine": engine, "asr_language": language})
    code = run_vc(["transcribe"] + argv)

    if code == 0:
        config.remember_file(source)
        _report_outputs([Path(output)])
    return code


def interactive_synthesize() -> int:
    """交互式：给视频加字幕。"""
    ui.section("给视频加字幕")

    video = ui.ask_path("视频文件", default=config.get("last_input"))
    subtitle = ui.ask_path("字幕文件（srt/ass）", default=_guess_subtitle(video))

    mode = ui.ask_choice(
        "字幕模式",
        [
            ("hard", "硬字幕 — 烧录进画面，任何播放器都能看到"),
            ("soft", "软字幕 — 嵌入字幕轨道，可开关"),
        ],
        default=config.get("subtitle_mode", "hard"),
    )

    argv = [video, "-s", subtitle, "--subtitle-mode", mode]

    if mode == "hard":
        quality = ui.ask_choice(
            "视频质量",
            [
                ("ultra", "极高 — CRF18，画质最好，文件最大"),
                ("high", "高 — CRF23"),
                ("medium", "中等 — CRF28，默认"),
                ("low", "低 — CRF32，文件最小"),
            ],
            default=config.get("video_quality", "medium"),
        )
        argv += ["--quality", quality]

        style = ui.ask(
            "样式预设（留空用默认，运行 vct style 可看全部）",
            default=config.get("subtitle_style", ""),
            allow_empty=True,
        )
        if style:
            argv += ["--style", style]
    else:
        quality = ""
        style = ""

    default_out = str(Path(video).with_name(Path(video).stem + "_字幕版.mp4"))
    output = _ask_output("输出视频", default_out)
    argv += ["-o", output]

    command_line = f"vct synthesize {' '.join(_quote(a) for a in argv)}"
    if not ui.confirm_command(command_line):
        return 0

    config.update({
        "subtitle_mode": mode,
        "subtitle_style": style,
        "video_quality": quality or config.get("video_quality"),
    })
    code = run_vc(["synthesize"] + argv)

    if code == 0:
        _report_outputs([Path(output)])
    return code


def interactive_subtitle() -> int:
    """交互式：字幕优化 / 翻译。"""
    ui.section("字幕优化 / 翻译")
    ui.hint("优化和断句需要 LLM API Key；翻译用必应/谷歌是免费的。")

    subtitle = ui.ask_path("字幕文件（srt/ass）", default=_guess_subtitle(config.get("last_input")))

    do_translate = ui.ask_yes_no("需要翻译成其他语言吗？", default=False)
    argv = [subtitle]

    translator = ""
    target = ""
    if do_translate:
        translator = ui.ask_choice(
            "翻译服务",
            [
                ("bing", "必应 — 免费，无需配置，推荐"),
                ("google", "谷歌 — 免费，无需配置"),
                ("llm", "大模型翻译 — 质量更好，需要 API Key"),
            ],
            default=config.get("translator", "bing"),
        )
        target = ui.ask(
            "目标语言代码（zh-Hans/en/ja/ko/fr/de…）",
            default=config.get("target_language", "zh-Hans"),
        )
        argv += ["--translator", translator, "--target-language", target]
    else:
        argv += ["--no-translate"]

    if not ui.ask_yes_no("启用 LLM 优化（修正错别字和标点）？需要 API Key", default=False):
        argv += ["--no-optimize"]
    if not ui.ask_yes_no("启用 LLM 断句（按语义重新断句）？需要 API Key", default=False):
        argv += ["--no-split"]

    default_out = str(Path(subtitle).with_name(Path(subtitle).stem + "_处理版" + Path(subtitle).suffix))
    output = _ask_output("输出字幕文件", default_out)
    argv += ["-o", output]

    command_line = f"vct subtitle {' '.join(_quote(a) for a in argv)}"
    if not ui.confirm_command(command_line):
        return 0

    config.update({"translator": translator or config.get("translator"),
                   "target_language": target or config.get("target_language")})
    code = run_vc(["subtitle"] + argv)

    if code == 0:
        _report_outputs([Path(output)])
    return code


def interactive_dub() -> int:
    """交互式：根据字幕生成配音。"""
    ui.section("生成配音")
    ui.hint("默认使用 Edge TTS，免费且无需 API Key，但需要联网。")

    subtitle = ui.ask_path("字幕文件（srt）", default=_guess_subtitle(config.get("last_input")))

    preset = ui.ask_choice(
        "配音预设",
        [
            ("edge-cn-female", "Edge 中文女声 — 免费，推荐"),
            ("edge-cn-male", "Edge 中文男声 — 免费"),
            ("edge-en-female", "Edge 英文女声 — 免费"),
            ("siliconflow-cn-female", "SiliconFlow 中文女声 — 需 API Key"),
            ("gemini-en-friendly", "Gemini 英文 — 需 API Key"),
        ],
        default="edge-cn-female",
    )

    argv = [subtitle, "--preset", preset]

    if preset.startswith("siliconflow") or preset.startswith("gemini"):
        api_key = ui.ask("TTS API Key", default="", allow_empty=True)
        if api_key:
            argv += ["--tts-api-key", api_key]

    want_video = ui.ask_yes_no("要把配音合成回视频吗？", default=False)
    if want_video:
        video = ui.ask_path("原视频文件", default=config.get("last_input"))
        default_out = str(Path(video).with_name(Path(video).stem + "_配音版.mp4"))
        argv += ["--video", video]
    else:
        video = ""
        default_out = str(Path(subtitle).with_suffix(".wav"))

    output = _ask_output("输出文件", default_out)
    argv += ["-o", output]

    command_line = f"vct dub {' '.join(_quote(a) for a in argv)}"
    if not ui.confirm_command(command_line):
        return 0

    code = run_vc(["dub"] + argv)

    if code == 0:
        _report_outputs([Path(output)])
    return code


def interactive_process() -> int:
    """交互式：全流程（转录 → 优化 → 翻译 → 合成）。"""
    ui.section("全流程处理")
    ui.hint("一条命令走完：转录 → 断句 → 优化 → 翻译 → 加字幕。")

    source = ui.ask_path("输入音视频文件", default=config.get("last_input"))

    engine = ui.ask_choice(
        "识别引擎",
        [
            ("bijian", "必剪 — 免费，中英文，推荐"),
            ("jianying", "剪映 — 免费，中英文"),
            ("whisper-api", "Whisper API — 需 API Key，支持多语种"),
        ],
        default=config.get("asr_engine", "bijian"),
    )
    argv = [source, "--asr", engine]

    do_translate = ui.ask_yes_no("需要翻译吗？", default=False)
    if do_translate:
        translator = ui.ask_choice(
            "翻译服务",
            [("bing", "必应 — 免费，推荐"), ("google", "谷歌 — 免费"), ("llm", "大模型 — 需 API Key")],
            default=config.get("translator", "bing"),
        )
        target = ui.ask("目标语言代码", default=config.get("target_language", "zh-Hans"))
        argv += ["--translator", translator, "--to", target]

    dub_only = ui.ask_yes_no("只输出配音视频（不烧录字幕）？", default=False)
    if dub_only:
        argv += ["--dub-only"]

    default_out = str(Path(source).with_name(Path(source).stem + "_成品.mp4"))
    output = _ask_output("输出文件", default_out)
    argv += ["-o", output]

    command_line = f"vct process {' '.join(_quote(a) for a in argv)}"
    if not ui.confirm_command(command_line):
        return 0

    config.update({"asr_engine": engine})
    code = run_vc(["process"] + argv)

    if code == 0:
        config.remember_file(source)
        _report_outputs([Path(output)])
    return code


def interactive_download() -> int:
    """交互式：下载在线视频。"""
    ui.section("下载在线视频")
    ui.hint("支持 YouTube、B 站等 yt-dlp 能处理的平台。")

    url = ui.ask("视频链接")
    default_dir = config.get("last_output_dir") or str(Path.home() / "Downloads")
    output_dir = ui.ask_path("保存目录", default=default_dir, must_exist=False, kind="dir")

    argv = [url, "-o", output_dir]

    command_line = f"vct download {' '.join(_quote(a) for a in argv)}"
    if not ui.confirm_command(command_line):
        return 0

    code = run_vc(["download"] + argv)

    if code == 0:
        config.set("last_output_dir", output_dir)
        ui.hint(f"文件已保存到：{output_dir}")
    return code


def interactive_style() -> int:
    """交互式：查看字幕样式预设（只读，不需要确认）。"""
    ui.section("字幕样式预设")
    return run_vc(["style"])


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _quote(text: str) -> str:
    """给含空格或特殊字符的参数加引号，仅用于回显。"""
    import shlex

    return shlex.quote(text)


def _guess_subtitle(video: str) -> str:
    """根据视频路径猜同名字幕文件，猜不到就返回空串。"""
    if not video:
        return ""
    candidate = Path(video).with_suffix(".srt")
    return str(candidate) if candidate.exists() else ""


def _report_outputs(paths: list[Path]) -> None:
    """打印产出清单。"""
    ui.blank()
    print("  " + ui.bold_text("产出文件："))
    for path in paths:
        if path.exists():
            ui.ok(ui.describe_file(path))
        else:
            # 有些命令的输出名由上游决定，这里不当作错误
            ui.warn(f"未在预期位置找到：{path}")
