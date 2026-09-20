"""
终端输出与交互模块。

集中处理三件事：
1. 带颜色的中文提示（info / ok / warn / error / step / header）
2. 交互式提问（文本、路径、选项、确认），带默认值和非法输入重问
3. macOS 拖拽路径清洗 —— 从 Finder 拖文件进终端会带上反斜杠转义和引号

提问优先使用 questionary 提供方向键选择菜单；questionary 缺失、stdin/stdout
不是终端、或终端窗口太小时，自动回退到纯标准库的文本输入 —— 所以管道、
重定向、脚本调用下的行为与改造前完全一致（见 rich_enabled）。

颜色在非 TTY（比如管道、重定向到文件）下自动关闭，
也遵守 NO_COLOR 环境变量约定。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# 颜色
# --------------------------------------------------------------------------

_COLOR_ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    """给文本套上 ANSI 颜色码；颜色关闭时原样返回。"""
    if not _COLOR_ENABLED:
        return text
    return f"\033[{code}m{text}\033[0m"


def _bold(text: str) -> str:
    return _c("1", text)


def _dim(text: str) -> str:
    return _c("2", text)


def bold_text(text: str) -> str:
    """加粗（公开版本，供其他模块拼接提示用）。"""
    return _bold(text)


def dim_text(text: str) -> str:
    """变暗（公开版本，供其他模块拼接提示用）。"""
    return _dim(text)


# 各状态用的配色：绿=成功、黄=警告、红=错误、青=信息、蓝=步骤
def green(text: str) -> str:
    return _c("32", text)


def yellow(text: str) -> str:
    return _c("33", text)


def red(text: str) -> str:
    return _c("31", text)


def cyan(text: str) -> str:
    return _c("36", text)


def blue(text: str) -> str:
    return _c("34", text)


def magenta(text: str) -> str:
    return _c("35", text)


# --------------------------------------------------------------------------
# 打印
# --------------------------------------------------------------------------

_WIDTH = 68


def blank() -> None:
    """输出一个空行。"""
    print()


def rule(char: str = "─") -> None:
    """输出一条分隔线。"""
    print(_dim(char * _WIDTH))


def header(title: str, subtitle: str = "") -> None:
    """输出带装饰的标题块。"""
    blank()
    rule("═")
    print("  " + _bold(title))
    if subtitle:
        print("  " + _dim(subtitle))
    rule("═")


def section(title: str) -> None:
    """输出小节标题。"""
    blank()
    print(_bold(cyan(f"▍ {title}")))


def info(message: str) -> None:
    print(f"  {cyan('·')} {message}")


def ok(message: str) -> None:
    print(f"  {green('✓')} {message}")


def warn(message: str) -> None:
    print(f"  {yellow('!')} {yellow(message)}")


def error(message: str) -> None:
    # 写 stderr 前先冲刷 stdout：输出被重定向到文件时 stdout 是块缓冲的，
    # 不冲刷的话错误信息会跑到前面去，看起来顺序全乱。
    sys.stdout.flush()
    print(f"  {red('✗')} {red(message)}", file=sys.stderr, flush=True)


def step(message: str) -> None:
    """输出一个执行步骤，形如 [1/2]。"""
    print(f"\n{blue('▶')} {_bold(message)}")


def hint(message: str) -> None:
    """输出缩进的补充说明，用于展示修复建议之类的多行文本。"""
    for line in message.splitlines():
        print("      " + _dim(line))


# --------------------------------------------------------------------------
# 机器可读的进度标记
# --------------------------------------------------------------------------

#: 进度标记的前缀。工作台（content-creation-workbench 的 backend/app/services/scene_runner.py）
#: 在切割时盯着这一行，把「第几条视频切到第几个片段」展示给用户。两边是同一份
#: 契约 —— 改格式要同时改那边的 PROGRESS_MARKER 正则。
#:
#: 为什么不直接让工作台去猜人类可读的输出：`[2/2] 切割 38 个片段` 这类文案是
#: 给人看的，措辞一变解析就悄悄失效；单镜头视频压根不打印那行，猜都无从猜起。
#: 标记行只此一处定义，解析方也只认它。
PROGRESS_PREFIX = "#vct-progress"

#: 阶段名：检测画面跳变 / 按切点切割。取值会原样落进工作台的数据库字段。
PHASE_DETECT = "detect"
PHASE_SPLIT = "split"


def progress(phase: str, done: int, total: int) -> None:
    """输出一行机器可读的进度标记。

    形如 `#vct-progress split 3 38`：第 3 个片段切完，总共要切 38 个。
    `total` 为 0 表示「这一步没有可切的东西」（比如单镜头视频），
    这是合法取值，调用方据此显示成「跳过切割」而不是「0/0 卡住了」。

    flush=True：工作台轮询的是被重定向到文件的 stdout，攒在缓冲区里就等于
    没有进度 —— 宁可多一次写系统调用。
    """
    print(f"{PROGRESS_PREFIX} {phase} {done} {total}", flush=True)


def bullet_list(items: list[str], marker: str = "•") -> None:
    """输出一个简单的项目符号列表。"""
    for item in items:
        print(f"  {cyan(marker)} {item}")


def kv_table(rows: list[tuple[str, str]], key_width: int = 14) -> None:
    """输出两列的对齐表格，第一列是键，第二列是值（值可含中文）。"""
    for key, value in rows:
        print(f"  {_bold(key.ljust(key_width))} {value}")


# --------------------------------------------------------------------------
# 路径处理
# --------------------------------------------------------------------------


def clean_path(raw: str) -> str:
    """清洗用户输入的路径。

    主要处理 macOS 拖拽文件到终端时产生的形式：
        /Users/me/my\\ file.mp4      ->  /Users/me/my file.mp4
        '/Users/me/my file.mp4'      ->  /Users/me/my file.mp4
        "~/Desktop/a.mp4"            ->  展开 ~
    同时去掉首尾空白，并把 ~ 展开成用户主目录。

    Args:
        raw: 用户原始输入。
    Returns:
        清洗后的路径字符串；输入为空时返回空串。
    """
    text = (raw or "").strip()
    if not text:
        return ""

    # 去掉成对包裹的引号，最多剥两层（有人会拖进来带引号又会手动加引号）
    for _ in range(2):
        if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
            text = text[1:-1].strip()

    # 还原反斜杠转义：`\ ` -> ` `，`\(` -> `(` 等。
    # 这是 macOS/Linux 拖拽进终端的产物，只在 POSIX 上做；Windows 的 `\`
    # 是路径分隔符，剥掉会把 E:\a\b.mp4 碾成盘符相对路径 E:ab.mp4，
    # resolve() 会把它拼到当前目录下，报「输入文件不存在」还看不出病因。
    if "\\" in text and os.name != "nt":
        text = re.sub(r"\\(.)", r"\1", text)

    # 展开 ~
    if text.startswith("~"):
        text = os.path.expanduser(text)

    return text


def human_size(num_bytes: int) -> str:
    """把字节数格式化成人类可读的形式，例如 '12.3 MB'。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def describe_file(path: Path) -> str:
    """给文件生成一句描述，形如 '12.3 MB — /path/to/file.mp4'。不存在时说明原因。"""
    try:
        stat = path.stat()
    except OSError:
        return f"（未生成）{path}"
    return f"{human_size(stat.st_size)} — {path}"


# --------------------------------------------------------------------------
# 交互后端探测
# --------------------------------------------------------------------------

# questionary 是可选依赖，导入一次约 180ms，而 `vct transcribe` 这类原样透传
# 给上游的命令根本用不到交互层，所以只在真正要提问时才导入，并且只尝试一次。
_QUESTIONARY = None
_QUESTIONARY_TRIED = False

# 选项标题里并进了副标题，最长一行接近 70 列，加上指针和缩进，
# 窗口太窄会折行、出现残影，还不如退回文本输入。
_MIN_TERMINAL_COLUMNS = 80
_MIN_TERMINAL_ROWS = 20


def _version_tuple(text: str) -> tuple[int, ...]:
    """把 '2.1.1' 解析成 (2, 1, 1)；解析不出数字的部分直接丢弃。"""
    parts: list[int] = []
    for chunk in str(text).split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def _load_questionary():
    """惰性导入 questionary，不可用时返回 None。

    刻意不写在模块顶层：装坏了、版本太旧都只该导致交互降级，
    不该让整个工具箱起不来，所以这里吞掉所有导入异常。

    Returns:
        questionary 模块；未安装 / 版本过低 / 导入出错时返回 None。
    """
    global _QUESTIONARY, _QUESTIONARY_TRIED
    if _QUESTIONARY_TRIED:
        return _QUESTIONARY
    _QUESTIONARY_TRIED = True

    try:
        import prompt_toolkit
        import questionary

        if _version_tuple(questionary.__version__) < (2, 0):
            return None
        if _version_tuple(prompt_toolkit.__version__) < (3, 0):
            return None
    except Exception:  # noqa: BLE001 —— 任何导入期异常都只意味着降级
        return None

    _QUESTIONARY = questionary
    return _QUESTIONARY


def _terminal_big_enough() -> bool:
    """终端窗口是否装得下带副标题的选项列表。"""
    try:
        size = shutil.get_terminal_size()
    except OSError:
        return False
    return (
        size.columns >= _MIN_TERMINAL_COLUMNS and size.lines >= _MIN_TERMINAL_ROWS
    )


def rich_enabled() -> bool:
    """当前是否启用方向键交互。

    每次调用都重新判定，而不是像 _COLOR_ENABLED 那样在导入时就定死 ——
    这样测试里可以 patch，用户放大窗口后重开菜单也能自动恢复。

    Returns:
        True 表示用 questionary 的方向键菜单，False 表示退回文本输入。
    """
    # 显式逃生口：自动判定万一出错，用户始终能用 VCT_PLAIN=1 强制关掉
    if os.environ.get("VCT_PLAIN"):
        return False

    # stdin 不是终端时 questionary 会抛 OSError(Errno 22)，
    # 必须在构造 Question 之前挡住。`vct < /dev/null`、管道、重定向都走这里。
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False

    if not _terminal_big_enough():
        return False

    return _load_questionary() is not None


# --------------------------------------------------------------------------
# questionary 封装
# --------------------------------------------------------------------------

# 「取消 / 返回」项的哨兵值。不能用 None —— questionary 会把 value=None
# 的选项悄悄换成它的标题字符串，导致哨兵永远匹配不上。
_CANCEL = "\x00__cancel__"

_Q_INSTRUCTION = "↑↓ 选择 · Enter 确认 · Ctrl-C 返回"


def _q_style():
    """questionary 的配色，尽量贴近本模块已有的 ANSI 风格。"""
    return _load_questionary().Style(
        [
            ("qmark", "fg:#5f819d"),             # 题干前的 ?，柔和的蓝
            ("question", "bold"),
            ("answer", "fg:#00afaf bold"),       # 确认后的答案回显
            ("pointer", "fg:#00afaf bold"),      # » 指针
            ("highlighted", "fg:#00afaf bold"),  # 当前高亮行
            ("selected", "fg:#00afaf"),
            ("instruction", "fg:#808080"),       # 灰色操作提示
            ("text", ""),
            ("placeholder", "fg:#808080"),       # 默认值的占位提示
        ]
    )


def _q_ask(question):
    """执行一次 questionary 提问，把取消语义统一成 Cancelled。

    全包只在这里调用 questionary 的提问方法，取消协议只有一份实现，
    与 _read_line 在纯文本路径里的角色对应。

    Args:
        question: questionary 的 Question 对象。
    Returns:
        用户的回答，保证不是 None。
    Raises:
        Cancelled: 用户按了 Ctrl-C / Ctrl-D。
    """
    try:
        # 用 unsafe_ask 而不是 ask：后者取消时既把 None 当正常返回值，
        # 又会额外打印一句英文 "Cancelled by user"。
        answer = question.unsafe_ask()
    except (KeyboardInterrupt, EOFError):
        print()
        raise Cancelled() from None
    except RuntimeError:
        # questionary 内部走 asyncio.run()，万一将来在已有事件循环里被调用会炸；
        # 当作取消处理，别把整个 CLI 带崩。
        print()
        raise Cancelled() from None

    if answer is None:
        raise Cancelled()
    return answer


def _safe_default(default, values: list):
    """把默认值收敛成 questionary 能接受的形式。

    questionary 在**构造** select 对象时就校验 default，只要它不在候选项里
    就直接抛 ValueError —— 而 default 常年来自配置文件，完全可能是上一版
    留下的过期值。收敛成 None 表示「不预选」。

    Args:
        default: 期望的默认值。
        values: 候选项的值列表。
    Returns:
        可用的默认值；无效时返回 None。
    """
    # 用 == 而不是 not default：ask_yes_no 的默认值可能是 False，
    # 而 False 是有意义的选项，不该被当成「没有默认值」。
    if default is None or default == "":
        return None
    return default if default in values else None


def _validate_text(text: str, has_default: bool, allow_empty: bool):
    """文本输入的校验函数。

    Returns:
        True 表示通过；否则返回中文错误提示。
    """
    if text.strip():
        return True
    if has_default or allow_empty:
        return True
    return "这一项不能为空（按 Ctrl-C 可取消）"


def _q_select(prompt: str, options, default="", allow_cancel: bool = True):
    """构造并执行一个方向键单选。

    Args:
        prompt: 提示语。
        options: [(值, 显示标签), ...]。
        default: 默认选中的值，不在候选项里时忽略。
        allow_cancel: 是否在末尾追加「取消 / 返回」项。
    Returns:
        被选中项的值。
    Raises:
        Cancelled: 用户按了 Ctrl-C，或选中了「取消 / 返回」。
    """
    questionary = _load_questionary()
    values = [value for value, _ in options]

    choices = [
        questionary.Choice(title=label, value=value) for value, label in options
    ]
    if allow_cancel:
        choices.append(questionary.Choice(title="← 取消 / 返回", value=_CANCEL))

    answer = _q_ask(
        questionary.select(
            f"  {prompt}",
            choices=choices,
            default=_safe_default(default, values),
            qmark="?",
            pointer="»",
            style=_q_style(),
            instruction=_Q_INSTRUCTION,
            # 搜索过滤会占用 j/k，关掉才能保住 vim 式的上下移动
            use_search_filter=False,
        )
    )
    if answer == _CANCEL:
        raise Cancelled()
    return answer


# --------------------------------------------------------------------------
# 交互提问
# --------------------------------------------------------------------------


class Cancelled(Exception):
    """用户在交互过程中主动取消（输入 q / quit / Ctrl-C）。"""


def _read_line(prompt: str) -> str:
    """读一行输入，把 Ctrl-C 和 Ctrl-D 统一转成 Cancelled。"""
    try:
        return input(prompt)
    except (KeyboardInterrupt, EOFError):
        print()
        raise Cancelled() from None


def _is_cancel(text: str) -> bool:
    return text.strip().lower() in ("q", "quit", "exit", "退出")


def ask(
    prompt: str,
    default: str = "",
    allow_empty: bool = False,
) -> str:
    """询问一段文本。

    Args:
        prompt: 提示语。
        default: 默认值，用户直接回车时返回它。
        allow_empty: 没有默认值时，是否允许空输入。
    Returns:
        用户输入（已 strip）。
    Raises:
        Cancelled: 用户输入 q/quit/exit 或按下 Ctrl-C。
    """
    if rich_enabled():
        # 默认值走 placeholder 而不是 default：questionary 的 default 会把内容
        # 预填进可编辑缓冲区，用户从访达拖进来的路径就会拼在旧默认值后面，
        # 变成「旧路径/新文件.mp4」。placeholder 只做灰色提示，回车才采用它。
        placeholder = f"默认：{default}（直接回车使用）" if default else ""
        answer = _q_ask(
            _load_questionary().text(
                f"  {prompt}",
                placeholder=placeholder,
                qmark="?",
                style=_q_style(),
                instruction=None,  # 文本题不挂一排英文操作提示
                validate=lambda text: _validate_text(text, bool(default), allow_empty),
            )
        ).strip()
        if _is_cancel(answer):
            raise Cancelled()
        return answer or default

    # ---- 以下是纯文本实现，非终端 / 未装 questionary / 窗口过小时使用 ----
    suffix = f" {_dim(f'[{default}]')}" if default else ""
    while True:
        raw = _read_line(f"  {prompt}{suffix}: ")
        if _is_cancel(raw):
            raise Cancelled()
        text = raw.strip()
        if text:
            return text
        if default:
            return default
        if allow_empty:
            return ""
        warn("这一项不能为空，请重新输入（输入 q 可取消）")


def ask_path(
    prompt: str,
    default: str = "",
    must_exist: bool = True,
    kind: str = "file",
) -> str:
    """询问一个路径，并校验存在性。

    Args:
        prompt: 提示语。
        default: 默认路径。
        must_exist: 是否要求路径必须已存在。
        kind: 'file' 要求是文件，'dir' 要求是目录，'any' 不限制。
    Returns:
        清洗后的绝对路径字符串。
    Raises:
        Cancelled: 用户取消。
    """
    while True:
        raw = ask(prompt, default=default)
        path = clean_path(raw)
        if not path:
            warn("路径不能为空")
            continue

        expanded = Path(path).expanduser()

        if not must_exist:
            # 不要求存在时仍然给出提示，但允许继续
            if kind == "dir":
                return str(expanded / "" if not str(expanded).endswith("/") else expanded)
            return str(expanded)

        if not expanded.exists():
            error(f"路径不存在：{expanded}")
            info("提示：把文件从访达拖进终端窗口即可自动填入路径")
            continue

        if kind == "file" and not expanded.is_file():
            error(f"这不是一个文件：{expanded}")
            continue

        if kind == "dir" and not expanded.is_dir():
            error(f"这不是一个目录：{expanded}")
            continue

        return str(expanded)


def ask_choice(
    prompt: str,
    options: list[tuple[str, str]],
    default: str = "",
    allow_cancel: bool = True,
) -> str:
    """让用户从若干选项里选一个。

    Args:
        prompt: 提示语。
        options: [(值, 中文说明), ...]，值用于返回，说明用于展示。
        default: 默认选项的值，不在候选项里时忽略。
        allow_cancel: 方向键模式下是否追加「取消 / 返回」项。顶层菜单
            自己有「退出」，再挂一个取消项反而让人犯嘀咕。
    Returns:
        被选中选项的「值」。
    Raises:
        Cancelled: 用户取消。
    """
    if rich_enabled():
        # 选项列表本身就是控件，不再预先打印一遍编号
        return _q_select(prompt, options, default, allow_cancel=allow_cancel)

    # ---- 以下是数字 / 值输入实现 ----
    blank()
    for index, (value, label) in enumerate(options, start=1):
        default_tag = _dim("  ← 默认") if value == default else ""
        print(f"    {cyan(str(index))}) {label}{default_tag}")
    if default:
        default_index = next(
            (str(i) for i, (v, _) in enumerate(options, 1) if v == default), ""
        )
        prompt_full = f"{prompt}（1-{len(options)}）"
    else:
        default_index = ""
        prompt_full = f"{prompt}（1-{len(options)}）"

    while True:
        raw = _read_line(f"  {prompt_full}: ")
        if _is_cancel(raw):
            raise Cancelled()
        text = raw.strip()
        if not text and default_index:
            return default
        if text.isdigit() and 1 <= int(text) <= len(options):
            return options[int(text) - 1][0]
        # 也允许直接输选项的值本身
        if text in {v for v, _ in options}:
            return text
        warn(f"请输入 1 到 {len(options)} 之间的数字")


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    """询问是/否。

    Args:
        prompt: 提示语。
        default: 直接回车时的答案。
    Returns:
        True 表示是，False 表示否。
    Raises:
        Cancelled: 用户取消。
    """
    if rich_enabled():
        # 候选项的值用字符串而不是 True/False：布尔值在 questionary 内部
        # 参与真假判断时容易踩坑，返回后再映射回 bool 最稳妥。
        answer = _q_select(
            prompt,
            [("yes", "是"), ("no", "否")],
            "yes" if default else "no",
        )
        return answer == "yes"

    # ---- 以下是 y/n 输入实现 ----
    tag = "Y/n" if default else "y/N"
    while True:
        raw = _read_line(f"  {prompt} {_dim(f'[{tag}]')}: ")
        text = raw.strip().lower()
        if not text:
            return default
        if text in ("y", "yes", "是", "1"):
            return True
        if text in ("n", "no", "否", "0"):
            return False
        if _is_cancel(text):
            raise Cancelled()
        warn("请输入 y 或 n")


def pause(prompt: str = "按任意键返回菜单") -> None:
    """暂停等待用户按键。用户按 Ctrl-C 也直接返回，不抛异常。

    这里绝不能向外抛 Cancelled：调用方（比如菜单主循环）靠它做「看完结果
    歇一口气」，抛出去会把整个循环带崩。
    """
    if rich_enabled():
        try:
            _load_questionary().press_any_key_to_continue(
                f"\n  {prompt} ...", style=_q_style()
            ).unsafe_ask()
        except (KeyboardInterrupt, EOFError, RuntimeError):
            print()
        return

    try:
        input(_dim(f"\n  {prompt} ..."))
    except (KeyboardInterrupt, EOFError):
        print()


def confirm_command(command_line: str) -> bool:
    """执行前回显完整命令，让用户确认。

    Args:
        command_line: 已经拼好的、可读的命令行字符串。
    Returns:
        True 表示用户确认执行。
    Raises:
        Cancelled: 用户取消。
    """
    blank()
    print("  " + _bold("即将执行以下命令："))
    blank()
    for line in command_line.splitlines():
        print("    " + cyan(line))
    blank()
    return ask_yes_no("确认执行？", default=True)
