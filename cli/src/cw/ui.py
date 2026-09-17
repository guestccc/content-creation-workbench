"""终端渲染。

所有「长什么样」的代码集中在这里，menu.py 只管流程。
用 rich 而不是手拼 ANSI 转义序列：表格对齐、宽字符（中文）的宽度计算、
颜色降级（输出被重定向时自动去掉颜色）都是别人已经踩过坑的问题。
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from cw.process import ServiceState, Status

console = Console()

_STATUS_STYLE = {
    Status.RUNNING_OWNED: ("● 运行中", "green"),
    Status.RUNNING_FOREIGN: ("● 运行中(外部)", "cyan"),
    Status.PORT_TAKEN: ("✗ 端口被占", "red"),
    Status.STOPPED: ("○ 未运行", "dim"),
    Status.STALE: ("◌ 记录残留", "yellow"),
}


def render_status(states: list[ServiceState]) -> None:
    """把当前所有服务的状态画成一张表。"""
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("服务", style="bold")
    table.add_column("状态")
    table.add_column("地址 / PID", overflow="fold")

    for state in states:
        text, style = _STATUS_STYLE[state.status]
        cell = f"[{style}]{text}[/{style}]"
        info_parts: list[str] = []
        if state.port is not None:
            info_parts.append(f":{state.port}")
        if state.handle is not None:
            info_parts.append(f"PID {state.handle.pid}")
        if state.detail:
            info_parts.append(state.detail)
        table.add_row(state.spec.label, cell, "  ".join(info_parts))

    console.print(table)


def print_ok(message: str) -> None:
    console.print(f"[green]✓[/green]  {message}")


def print_err(message: str) -> None:
    console.print(f"[red]✗[/red]  {message}")


def print_warn(message: str) -> None:
    console.print(f"[yellow]![/yellow]  {message}")


def print_info(message: str) -> None:
    console.print(f"[dim]{message}[/dim]")
