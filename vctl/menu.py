"""
中文交互式菜单。

不带参数运行 `vct` 时进入这里。目标是：不记命令、不查文档也能干活。

菜单顶部会显示一行环境状态，让用户一眼看出「哪些功能现在能用」，
免得选了半天才发现环境没装。

每次执行完回到菜单，而不是直接退出 —— 处理视频通常是连着做几件事
（先擦字幕，再加新字幕，再导出），不用反复敲命令。
"""

from __future__ import annotations

from typing import Callable

from . import TOOL_CMD, TOOL_NAME, __version__, config, env, ui


def _status_line() -> str:
    """拼出菜单顶部那行环境状态。"""
    vc = env.probe_videocaptioner()
    vsr = env.probe_vsr()
    ffmpeg = env.probe_ffmpeg()
    scene = env.probe_scenedetect()

    def mark(ok: bool, name: str) -> str:
        return ui.green(f"● {name}") if ok else ui.yellow(f"○ {name}")

    parts = [
        mark(vc.ok, "字幕功能"),
        mark(vsr.ok, "去字幕"),
        mark(ffmpeg.ok, "ffmpeg"),
        mark(scene.ok, "镜头分割"),
    ]
    line = "  ".join(parts)

    if not (vc.ok and vsr.ok and ffmpeg.ok and scene.ok):
        line += ui.dim_text("    （○ 表示未就绪，选『环境诊断』查看怎么修）")
    return line


def _last_file_line() -> str:
    """拼出「上次处理的文件」提示行，没有就返回空串。"""
    last = config.get("last_input", "")
    if not last:
        return ""
    from pathlib import Path

    name = Path(last).name
    exists = "  " if Path(last).exists() else ui.yellow("（已不在原位置）")
    return "  " + ui.dim_text(f"上次处理：{name}{exists}")


# 菜单项：(编号, 标题, 副标题, 处理函数名)
# 处理函数用字符串延迟导入，避免菜单模块一加载就把所有命令模块拉起来
MENU_ITEMS: list[tuple[str, str, str, str]] = [
    ("1", "提取字幕 + 擦除字幕", "推荐 · 一次拿到文案和干净画面", "combo.interactive_extract_desub"),
    ("2", "智能镜头分割", "把多镜头素材按画面自动拆成片段", "scene.interactive_scene"),
    ("3", "语音转字幕（ASR）", "视频/音频转文字，免费", "vc.interactive_transcribe"),
    ("4", "擦除硬字幕", "去掉画面里原有的字", "vsr.interactive_desub"),
    ("5", "给视频加字幕", "烧录或嵌入自己的字幕", "vc.interactive_synthesize"),
    ("6", "字幕优化 / 翻译", "润色、断句、翻译成其他语言", "vc.interactive_subtitle"),
    ("7", "生成配音", "根据字幕生成配音音轨或配音视频", "vc.interactive_dub"),
    ("8", "全流程处理", "转录 → 翻译 → 合成，一条命令走完", "vc.interactive_process"),
    ("9", "下载在线视频", "支持 YouTube、B 站等", "vc.interactive_download"),
    ("10", "查看字幕样式预设", "看看有哪些好看的样式可以选", "vc.interactive_style"),
    ("11", "打开图形界面", "VideoCaptioner 桌面版", "vc_gui"),
    ("12", "环境诊断", "检查两个工具是否就绪", "doctor.interactive_doctor"),
    ("13", "安装去字幕环境", "video-subtitle-remover 依赖安装引导", "setup_vsr.interactive_setup"),
    ("14", "配置管理", "查看和修改 vct 的默认参数", "config_menu"),
]


def _resolve(name: str) -> Callable[[], int]:
    """把 'combo.interactive_extract_desub' 这样的字符串解析成可调用对象。"""
    if name == "vc_gui":
        from .commands import vc

        return vc.launch_gui
    if name == "config_menu":
        return config_menu

    module_name, func_name = name.split(".", 1)
    from importlib import import_module

    module = import_module(f".commands.{module_name}", package="vctl")
    return getattr(module, func_name)


def _menu_items() -> list[tuple[str, str]]:
    """主菜单的候选项 [(编号, 显示标签), ...]，末尾附「退出」。

    副标题并进标题同一行 —— questionary 的选项只支持单行标题。
    抽成纯函数是为了好单测（见 vctl/tests/test_menu.py）。
    """
    items = [
        (number, f"{title} · {subtitle}")
        for number, title, subtitle, _ in MENU_ITEMS
    ]
    items.append(("0", "退出"))
    return items


def _draw_header() -> None:
    """画出标题栏和当前环境状态。

    菜单选项本身由 ui.ask_choice 渲染，这里不再打印一遍 —— 否则
    方向键菜单会和静态列表叠在一起，出现两份。
    """
    ui.blank()
    ui.rule("═")
    print(f"  {ui.bold_text(TOOL_NAME + ' ' + TOOL_CMD)}  {ui.dim_text('v' + __version__)}")
    ui.rule("═")
    ui.blank()

    print(_status_line())
    last_line = _last_file_line()
    if last_line:
        print(last_line)

    ui.blank()


def main_menu() -> int:
    """菜单主循环。一直跑，直到用户选「退出」或按 Ctrl-C。

    Returns:
        退出码，正常退出为 0。
    """
    # 编号 -> 处理函数名
    lookup = {number: handler for number, _, _, handler in MENU_ITEMS}

    while True:
        _draw_header()

        try:
            # allow_cancel=False：顶层已经有「退出」项，再挂一个取消项没意义
            choice = ui.ask_choice("请选择功能", _menu_items(), allow_cancel=False)
        except ui.Cancelled:
            ui.blank()
            ui.info("再见。")
            return 0

        if choice == "0":
            ui.blank()
            ui.info("再见。")
            return 0

        handler_name = lookup.get(choice)
        if handler_name is None:
            # 方向键只可能选中候选项，这里是给简单输入模式兜底的防御分支
            ui.blank()
            ui.warn(f"没有 {choice} 这个选项，请重新选择")
            continue

        # ---- 执行选中的功能 ----
        try:
            handler = _resolve(handler_name)
            handler()
        except ui.Cancelled:
            ui.blank()
            ui.info("已取消，返回菜单。")
        except KeyboardInterrupt:
            ui.blank()
            ui.info("已中断，返回菜单。")
        except Exception as exc:  # noqa: BLE001
            # 单个功能出错不该把整个菜单带崩
            ui.blank()
            ui.error(f"这个功能执行时出错：{exc}")
            ui.hint("可以运行菜单里的「环境诊断」排查，或直接看上面的错误信息。")

        ui.pause()


# --------------------------------------------------------------------------
# 配置管理子菜单
# --------------------------------------------------------------------------


# 可以改的配置项：(配置键, 中文名, 可选值说明)
EDITABLE_CONFIG: list[tuple[str, str, str]] = [
    ("asr_engine", "默认识别引擎", "bijian / jianying / whisper-api / whisper-cpp"),
    ("asr_language", "默认源语言", "auto / zh / en / ja …"),
    ("translator", "默认翻译服务", "bing / google / llm"),
    ("target_language", "默认目标语言", "zh-Hans / en / ja …"),
    ("desub_mode", "默认擦除算法", "sttn-auto / sttn-det / lama / propainter / opencv"),
    ("desub_area", "默认擦除区域", "bottom / full / top / custom"),
    ("subtitle_mode", "默认字幕模式", "hard / soft"),
    ("subtitle_style", "默认字幕样式", "预设名，留空用默认"),
    ("video_quality", "默认视频质量", "ultra / high / medium / low"),
]


def config_menu() -> int:
    """配置管理子菜单。"""
    while True:
        ui.header("配置管理", f"配置文件：{config.CONFIG_PATH}")

        ui.section("当前设置")
        rows = [(label, str(config.get(key, "")) or ui.dim_text("（未设置）"))
                for key, label, _ in EDITABLE_CONFIG]
        ui.kv_table(rows, key_width=16)

        ui.blank()
        ui.info(f"最近处理过 {len(config.recent_files())} 个文件")

        ui.blank()
        try:
            choice = ui.ask_choice("请选择", [
                ("1", "修改某一项"),
                ("2", "重置为默认值"),
                ("3", "显示配置文件路径"),
                ("0", "返回主菜单"),
            ])
        except ui.Cancelled:
            return 0

        if choice == "0":
            return 0

        if choice == "1":
            _edit_one()
        elif choice == "2":
            if ui.ask_yes_no("确认把所有设置恢复为默认值？", default=False):
                config.reset()
                ui.ok("已恢复默认值。")
        elif choice == "3":
            ui.blank()
            ui.info(str(config.CONFIG_PATH))
            if config.CONFIG_PATH.exists():
                ui.hint("可以用文本编辑器打开它手动修改，改完记得重启 vct。")
            else:
                ui.hint("这个文件还没创建 —— 改任意一项设置后就会自动生成。")
        else:
            ui.warn(f"没有 {choice} 这个选项")


def _edit_one() -> None:
    """选择并修改一个配置项。"""
    # 候选项的值直接用配置键本身，不再靠序号换算 —— 选错项的可能性归零
    try:
        key = ui.ask_choice(
            "要修改哪一项",
            [
                (key, f"{label} · 当前：{str(config.get(key, '') or '未设置')}")
                for key, label, _ in EDITABLE_CONFIG
            ],
        )
    except ui.Cancelled:
        return

    label, options = next(
        (label, options) for k, label, options in EDITABLE_CONFIG if k == key
    )
    ui.blank()
    ui.hint(f"可选值：{options}")
    try:
        value = ui.ask(f"新的{label}", default=str(config.get(key, "")))
    except ui.Cancelled:
        return

    config.set(key, value)
    ui.ok(f"{label} 已设为：{value}")
