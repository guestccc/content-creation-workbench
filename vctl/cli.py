"""
命令行入口与参数分发。

设计要点：**VideoCaptioner 的子命令一律原样透传**。

`vct transcribe video.mp4 --asr bijian` 实际执行的是
    <VideoCaptioner>/.venv/bin/python -m videocaptioner.cli.main transcribe video.mp4 --asr bijian

也就是说 vct 不复制上游的参数定义。好处是上游升级新增参数时 vct 自动支持，
坏处是 `vct transcribe --help` 显示的是上游的帮助（这恰恰是我们想要的）。

vct 自己解析的只有这些子命令：
    scene           智能镜头分割（按画面切分素材）
    desub           擦除硬字幕
    extract-desub   提取字幕 + 擦除字幕
    doctor          环境诊断
    setup-vsr       安装去字幕环境
    menu            打开交互式菜单
    gui             打开 VideoCaptioner 图形界面
    vc              原样透传给 VideoCaptioner（逃生口）
"""

from __future__ import annotations

import argparse
import os
import sys

from . import TOOL_CMD, TOOL_NAME, __version__, env, ui

# 这些子命令原样转发给 VideoCaptioner
PASSTHROUGH = {
    "transcribe": "语音转字幕（ASR）",
    "subtitle": "字幕优化 / 翻译",
    "synthesize": "字幕合成到视频",
    "dub": "根据字幕生成配音",
    "process": "全流程：转录 → 优化 → 翻译 → 合成",
    "download": "下载在线视频",
    "style": "查看字幕样式预设",
    "config": "管理 VideoCaptioner 配置",
}

# vct 自己实现的子命令，用作菜单里显示的顺序
NATIVE = {
    "extract-desub": "提取字幕 + 擦除字幕（组合流程）",
    "scene": "智能镜头分割（按画面切分素材）",
    "desub": "擦除视频里的硬字幕 / 水印",
    "doctor": "环境诊断",
    "setup-vsr": "安装去字幕工具的运行环境",
    "menu": "打开交互式菜单",
    "gui": "打开 VideoCaptioner 图形界面",
    "vc": "原样透传参数给 VideoCaptioner",
}


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """构建顶层参数解析器。"""
    parser = argparse.ArgumentParser(
        prog=TOOL_CMD,
        description=(
            f"{TOOL_NAME} — 把 VideoCaptioner 和 video-subtitle-remover 合成一个入口。\n"
            "不带任何参数运行会打开中文交互式菜单。"
        ),
        epilog=(
            "示例：\n"
            f"  {TOOL_CMD}                              打开交互式菜单\n"
            f"  {TOOL_CMD} extract-desub 视频.mp4       提取字幕 + 擦除硬字幕\n"
            f"  {TOOL_CMD} transcribe 视频.mp4           语音转字幕（免费）\n"
            f"  {TOOL_CMD} desub 视频.mp4                擦除硬字幕\n"
            f"  {TOOL_CMD} synthesize 视频.mp4 -s 字幕.srt   给视频加字幕\n"
            f"  {TOOL_CMD} doctor                        检查环境\n"
            f"\n"
            f"其他 VideoCaptioner 子命令（subtitle / dub / process / download / style / config）\n"
            f"的参数会原样转发，用 '{TOOL_CMD} <子命令> --help' 查看上游帮助。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"{TOOL_CMD} {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<子命令>")

    # ---- 原样透传给 VideoCaptioner 的子命令 ----
    # add_help=False 是故意的：这样 -h/--help 会被当成普通参数转发给上游，
    # 用户看到的就是 VideoCaptioner 自己的帮助，而不是 vct 的。
    for name, help_text in PASSTHROUGH.items():
        sub = subparsers.add_parser(
            name,
            add_help=False,
            help=help_text,
            description=f"转发给 VideoCaptioner：{help_text}（参数原样透传）",
        )
        sub.add_argument("args", nargs=argparse.REMAINDER, help="原样转发给 VideoCaptioner 的参数")

    # ---- 原样透传的逃生口 ----
    raw = subparsers.add_parser(
        "vc",
        add_help=False,
        help=NATIVE["vc"],
        description="把参数原样传给 VideoCaptioner CLI，不做任何加工",
    )
    raw.add_argument("args", nargs=argparse.REMAINDER)

    # ---- vct 自己实现的子命令 ----
    from .commands import combo, doctor, scene, setup_vsr, vsr

    sub = subparsers.add_parser(
        "scene",
        help=NATIVE["scene"],
        description="按画面跳变把多镜头视频自动拆成单镜头片段（PySceneDetect）",
    )
    scene.add_arguments(sub)

    sub = subparsers.add_parser(
        "desub",
        help=NATIVE["desub"],
        description="擦除视频里的硬字幕或水印（video-subtitle-remover）",
    )
    vsr.add_arguments(sub)

    sub = subparsers.add_parser(
        "extract-desub",
        help=NATIVE["extract-desub"],
        description="一次做完：语音转字幕（拿文案）+ 擦除硬字幕（拿干净画面）",
    )
    combo.add_arguments(sub)

    sub = subparsers.add_parser(
        "doctor",
        help=NATIVE["doctor"],
        description="检查两个工具的运行环境和功能可用性",
    )
    doctor.add_arguments(sub)

    sub = subparsers.add_parser(
        "setup-vsr",
        help=NATIVE["setup-vsr"],
        description="安装 video-subtitle-remover 的 Python 依赖",
    )
    setup_vsr.add_arguments(sub)

    subparsers.add_parser(
        "menu",
        help=NATIVE["menu"],
        description="打开中文交互式菜单",
    )

    subparsers.add_parser(
        "gui",
        help=NATIVE["gui"],
        description="打开 VideoCaptioner 的图形界面",
    )

    return parser


# --------------------------------------------------------------------------
# 分发
# --------------------------------------------------------------------------


def _extract_dry_run(argv: list[str]) -> tuple[list[str], bool]:
    """从透传参数里挑出 vct 自己的 --dry-run 开关。

    上游没有 --dry-run 这个选项，所以可以安全地在这里摘掉，
    让用户既能预览命令、又不影响转发给上游的参数。
    """
    dry_run = False
    cleaned = []
    for item in argv:
        if item == "--dry-run":
            dry_run = True
        else:
            cleaned.append(item)
    return cleaned, dry_run


def dispatch(args: argparse.Namespace) -> int:
    """根据解析结果调用对应的处理函数，返回进程退出码。"""
    command = args.command

    # ---- 不带参数：打开交互式菜单 ----
    if command is None:
        from . import menu

        return menu.main_menu()

    # ---- 原样透传 ----
    if command in PASSTHROUGH or command == "vc":
        from .commands import vc

        forwarded, dry_run = _extract_dry_run(list(args.args))

        if command == "vc":
            if not forwarded:
                ui.error(f"用法：{TOOL_CMD} vc <VideoCaptioner 的参数>")
                ui.hint(f"例如：{TOOL_CMD} vc transcribe video.mp4 --asr bijian")
                return 2
            return vc.run_vc(forwarded, dry_run=dry_run)

        if not forwarded:
            # 用户只敲了子命令，没给其他参数：顺手把上游帮助显示出来
            return vc.run_vc([command, "--help"])
        return vc.run_vc([command] + forwarded, dry_run=dry_run)

    # ---- vct 自己实现的子命令 ----
    if command == "desub":
        from .commands import vsr

        return vsr.run(args)

    if command == "extract-desub":
        from .commands import combo

        return combo.run(args)

    if command == "scene":
        from .commands import scene

        return scene.run(args)

    if command == "doctor":
        from .commands import doctor

        return doctor.run(args)

    if command == "setup-vsr":
        from .commands import setup_vsr

        return setup_vsr.run(args)

    if command == "gui":
        from .commands import vc

        return vc.launch_gui()

    if command == "menu":
        from . import menu

        return menu.main_menu()

    ui.error(f"未知的子命令：{command}")
    return 2


def _try_passthrough(argv: list[str]) -> int | None:
    """透传类子命令的快速通道：完全绕过 argparse。

    为什么需要它？argparse 的 nargs=REMAINDER 只在遇到第一个「不像选项」的
    参数之后才开始吞参数，所以 `vct transcribe --help` 里的 --help 会被
    argparse 当成 vct 自己的选项，直接报 "unrecognized arguments" 退出，
    根本转发不到上游。而看上游帮助恰恰是用户最常用的动作之一。

    与其和 argparse 的这条规则较劲，不如让透传子命令一开始就不过 argparse：
    只要第一个参数是透传子命令名，剩下的原样交给上游。

    Args:
        argv: 原始参数列表（不含程序名）。
    Returns:
        处理完毕则返回退出码；不是透传子命令则返回 None，交给 argparse。
    """
    if not argv or (argv[0] not in PASSTHROUGH and argv[0] != "vc"):
        return None

    from .commands import vc

    name, rest = argv[0], list(argv[1:])

    # vct 的 --dry-run 不是上游的参数，先摘出来再用
    rest, dry_run = _extract_dry_run(rest)

    if name == "vc":
        if not rest:
            ui.error(f"用法：{TOOL_CMD} vc <VideoCaptioner 的参数>")
            ui.hint(f"例如：{TOOL_CMD} vc transcribe video.mp4 --asr bijian")
            return 2
        return vc.run_vc(rest, dry_run=dry_run)

    if not rest:
        # 只敲了子命令没给参数：顺手把上游帮助显示出来，省得用户再敲一次
        return vc.run_vc([name, "--help"])

    return vc.run_vc([name] + rest, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。

    Args:
        argv: 参数列表，默认取 sys.argv[1:]。
    Returns:
        进程退出码。约定：0 成功、2 参数错误、3 输入不存在、4 依赖缺失、5 运行时失败。
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        # 透传类子命令先走快速通道，绕开 argparse 对 --help 的拦截
        passthrough_code = _try_passthrough(argv)
        if passthrough_code is not None:
            return passthrough_code

        parser = build_parser()
        args = parser.parse_args(argv)
        return dispatch(args)
    except KeyboardInterrupt:
        ui.blank()
        ui.warn("已取消")
        return 130
    except BrokenPipeError:
        # 输出被管道截断（比如 `vct style | head`），正常退出即可
        try:
            sys.stdout.close()
        except OSError:
            pass
        return 0
    except ui.Cancelled:
        ui.blank()
        ui.warn("已取消")
        return 130
    except Exception as exc:  # noqa: BLE001
        # 兜底：任何未预料的异常都要给出人能看懂的信息，而不是一堆堆栈
        ui.blank()
        ui.error(f"执行时出错：{exc}")
        ui.hint(
            "这多半是环境或参数问题。可以运行 vct doctor 检查环境；\n"
            "需要看详细堆栈的话，设置环境变量 VCT_DEBUG=1 再运行一次。"
        )
        if os.environ.get("VCT_DEBUG"):
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
