"""CLI 入口。

只负责构造 Typer 应用并调用它。具体交互流程在 menu.py 里。

关于退出码：Typer 的默认行为已经覆盖了所有需要的情况，不需要自己兜异常——
  - Ctrl+C         → 内部转成 Exit(130)，退出码 130（实测确认，不会打印 Aborted!）
  - 参数用法错误    → 打印用法提示，退出码 2
  - --help         → 退出码 0
注意不要改成 standalone_mode=False：那种模式下 Typer 会把退出码「返回」而不是
sys.exit()，调用方一旦忽略返回值，Ctrl+C 就会变成退出码 0。
"""

import sys

import typer
from rich.console import Console

from cw import __version__

console = Console()

# - add_completion=False：本项目通过仓库根目录的 cw 脚本调用，装 shell 补全没有意义
# - rich_markup_mode=None：Typer 默认按 rich 标签解析 help 文本，
#   中文说明里的方括号会被当成标签吃掉甚至报错，这里关掉
# - no_args_is_help=False：不带参数时要直接进交互菜单，而不是打印帮助
app = typer.Typer(
    add_completion=False,
    rich_markup_mode=None,
    no_args_is_help=False,
    help="内容创作工作台 - 开发服务管理",
)


def _show_version(value: bool) -> None:
    """--version 的回调。"""
    if value:
        console.print(f"内容创作工作台 CLI {__version__}")
        raise typer.Exit()


# 只定义这一个命令，且不加 callback：Typer 在「只有一个命令」时会把它折叠到根层级，
# 于是 `cw` 直接执行它，同时 `cw --help` / `cw --version` 依然可用。
# 如果改成「callback + 命令」的写法，`cw` 将只触发 callback 而不会进入菜单。
@app.command()
def run(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="显示版本号后退出",
        callback=_show_version,
        is_eager=True,
    ),
) -> None:
    """选择并管理开发服务。"""
    # 菜单依赖方向键交互，没有 TTY 时 questionary 会直接报错或挂死。
    # 这个检查放在延迟导入之前，让非交互场景不依赖交互库能否加载
    if not sys.stdin.isatty():
        console.print("[red]✗[/red]  需要交互式终端。请在终端中直接运行 cw。")
        raise typer.Exit(1)

    # 延迟导入：交互库（questionary / prompt-toolkit）只在真正要进入菜单时才加载，
    # 这样 --help、--version 这些路径不必付出加载终端交互库的代价
    from cw.menu import menu_loop
    from cw.services import find_root

    menu_loop(find_root())


def main() -> None:
    """[project.scripts] 的入口。"""
    app()


if __name__ == "__main__":
    main()
