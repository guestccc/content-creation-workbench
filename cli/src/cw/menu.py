"""交互菜单与启动/停止流程。

这是所有模块的组装点：菜单结构、前后台分派、启动后的就绪探测与提示。
模块依赖方向是单向的：menu → ui / process / readiness / services / envfile，
下层模块之间不互相 import（process 与 readiness 之间唯一的交集是
group_listen_ports，从 process 单向取用）。

流程上有意做成「每次回到主菜单都重新探测状态」：
状态可能随时被别的终端改变（用户手动 kill、另一个 cw 实例），
缓存的结果比慢一点的实时结果更害人。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import psutil
import questionary
from questionary import Choice

from cw.envfile import resolve_bind
from cw.errors import ConfigError, PrecheckError, ProcessError
from cw.logs import tail_lines
from cw.net import normalize_probe_host
from cw.process import (
    Handle,
    ServiceState,
    Status,
    StopOutcome,
    clear_handle,
    determine_state,
    ensure_dirs,
    logs_dir,
    pidfile_path,
    port_owner,
    spawn_background,
    spawn_foreground,
    stop_group,
    wait_foreground,
    write_handle,
)
from cw.readiness import wait_ready
from cw.services import (
    ServiceSpec,
    backend_python,
    build_services,
    check_proxy_consistency,
    full_start_order,
)
from cw.ui import console, print_err, print_info, print_ok, print_warn, render_status

# 主菜单里的固定条目 id
_ALL = "__all__"
_QUIT = "__quit__"

# 二级菜单动作 id
_ACT_FOREGROUND = "foreground"
_ACT_BACKGROUND = "background"
_ACT_STOP = "stop"
_ACT_LOGS = "logs"
_ACT_CLEAR = "clear"
_ACT_BACK = "back"


def menu_loop(root: Path) -> None:
    """主循环：显示状态 → 选择服务 → 执行动作 → 回到状态页。"""
    try:
        bind = resolve_bind(root / "backend" / ".env")
    except ConfigError as exc:
        print_err(str(exc))
        raise SystemExit(1) from exc

    specs = build_services(root, bind)

    try:
        ensure_dirs(root)
    except ProcessError as exc:
        print_err(str(exc))
        raise SystemExit(1) from exc

    warning = check_proxy_consistency(root, bind[1])
    if warning:
        print_warn(warning)
        console.print()

    while True:
        states = [determine_state(spec, root) for spec in specs.values()]
        choice = _ask_service(states)
        if choice is None or choice == _QUIT:
            return
        if choice == _ALL:
            _start_all(root, specs, bind)
            continue
        _service_menu(root, specs[choice], bind)


def _ask_service(states: list[ServiceState]) -> str | None:
    """一级菜单：服务列表 + 内联状态。返回服务 id 或固定条目 id。"""
    console.print()
    render_status(states)
    console.print()

    choices: list[Choice] = []
    for state in states:
        badge = "●" if state.status in (Status.RUNNING_OWNED, Status.RUNNING_FOREIGN) else "○"
        choices.append(Choice(
            title=f"{badge} {state.spec.label}",
            value=state.spec.id,
        ))
    choices.append(Choice(title="────────────", disabled=" "))
    choices.append(Choice(title="全部启动（后台）", value=_ALL))
    choices.append(Choice(title="退出", value=_QUIT))

    return questionary.select(
        "请选择服务（↑↓ 移动，回车确认，Ctrl+C 退出）",
        choices=choices,
        use_jk_keys=False,
    ).unsafe_ask()


def _service_menu(root: Path, spec: ServiceSpec, bind: tuple[str, int]) -> None:
    """二级菜单：根据当前状态给出可用的动作。"""
    while True:
        state = determine_state(spec, root)

        actions: list[Choice] = []
        if state.status in (Status.STOPPED, Status.STALE):
            actions.append(Choice("前台运行（Ctrl+C 停止）", value=_ACT_FOREGROUND))
            actions.append(Choice("后台运行并返回", value=_ACT_BACKGROUND))
        if state.status == Status.STALE:
            actions.append(Choice("清理残留记录", value=_ACT_CLEAR))
        if state.status == Status.RUNNING_OWNED:
            actions.append(Choice("查看日志", value=_ACT_LOGS))
            actions.append(Choice("停止服务", value=_ACT_STOP))
        if state.status == Status.RUNNING_FOREIGN:
            actions.append(Choice("查看日志", value=_ACT_LOGS))
        actions.append(Choice("返回", value=_ACT_BACK))

        header = _describe_state(state)
        action = questionary.select(
            f"{spec.label} — {header}",
            choices=actions,
            use_jk_keys=False,
        ).unsafe_ask()

        if action is None or action == _ACT_BACK:
            return
        if action == _ACT_FOREGROUND:
            _run_foreground(root, spec, bind)
            return  # 前台结束后直接回主菜单，状态已经变了
        if action == _ACT_BACKGROUND:
            _run_background(root, spec, bind)
            return
        if action == _ACT_STOP:
            _stop(root, spec, state.handle)
        if action == _ACT_LOGS:
            _show_logs(root, spec)
        if action == _ACT_CLEAR:
            clear_handle(pidfile_path(root, spec.id))
            print_ok("残留记录已清理")


def _describe_state(state: ServiceState) -> str:
    """二级菜单标题里的一句话状态。"""
    if state.status == Status.RUNNING_OWNED:
        return f"运行中，PID {state.handle.pid}"
    if state.status == Status.RUNNING_FOREIGN:
        return "运行中（非本工具启动，无法从这里停止）"
    if state.status == Status.PORT_TAKEN:
        return f"端口被占用：{state.detail}"
    if state.status == Status.STALE:
        return "上次启动的进程已退出，但留下了记录"
    return "未运行"


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------


def _precheck(spec: ServiceSpec) -> None:
    """启动前的环境检查，失败抛 PrecheckError。

    按用户的要求本工具不自动安装依赖，但要在启动前把「缺什么、去哪装」
    说清楚，而不是让服务进程在后台悄悄死掉。
    """
    if spec.id == "backend":
        python = backend_python(spec.cwd.parent)
        if not python.exists():
            raise PrecheckError(
                "后端虚拟环境不存在。请先执行：\n"
                "  cd backend && uv sync"
            )
    else:
        if shutil.which("npm") is None:
            raise PrecheckError("找不到 npm。请先安装 Node.js（https://nodejs.org）")
        if not (spec.cwd / "node_modules").is_dir():
            raise PrecheckError(
                f"{spec.label}的依赖还没安装。请先执行：\n"
                f"  cd {spec.cwd.name} && npm install"
            )


def _run_background(root: Path, spec: ServiceSpec, bind: tuple[str, int]) -> None:
    """后台启动一个服务并等待就绪。"""
    try:
        _precheck(spec)
    except PrecheckError as exc:
        print_err(str(exc))
        return

    log_file = logs_dir(root) / f"{spec.id}.log"
    print_info(f"正在后台启动{spec.label}…（日志 {log_file}）")

    try:
        handle = spawn_background(spec, log_file)
        write_handle(pidfile_path(root, spec.id), handle)
    except ProcessError as exc:
        print_err(str(exc))
        return

    _await_ready(spec, handle, bind)


def _run_foreground(root: Path, spec: ServiceSpec, bind: tuple[str, int]) -> None:
    """前台运行：服务的输出直接打到当前终端，Ctrl+C 停止。

    即使是前台运行也写一条 pidfile——否则在另一个终端里看这个服务
    会被误判成「外部启动」，停止入口也对它完全失明。
    """
    try:
        _precheck(spec)
    except PrecheckError as exc:
        print_err(str(exc))
        return

    print_info(f"前台运行{spec.label}，Ctrl+C 停止并返回菜单")
    console.print()

    try:
        handle, proc = spawn_foreground(spec)
        write_handle(pidfile_path(root, spec.id), handle)
    except ProcessError as exc:
        print_err(str(exc))
        return

    try:
        exit_code = wait_foreground(proc)
    finally:
        clear_handle(pidfile_path(root, spec.id))

    console.print()
    if exit_code == 0:
        print_ok(f"{spec.label}已退出")
    else:
        print_warn(f"{spec.label}已退出，退出码 {exit_code}")


def _start_all(root: Path, specs: dict[str, ServiceSpec], bind: tuple[str, int]) -> None:
    """按依赖顺序后台启动全部服务。

    「全部启动」只提供后台模式：前台运行一次只能承载一个服务，
    这个语义矛盾不该留给用户去撞。
    """
    already = [
        spec.id for spec in full_start_order(specs)
        if determine_state(spec, root).status in (Status.RUNNING_OWNED, Status.RUNNING_FOREIGN)
    ]
    if already:
        names = "、".join(specs[sid].label for sid in already)
        print_warn(f"{names} 已在运行，将跳过")

    for spec in full_start_order(specs):
        if spec.id in already:
            continue
        _run_background(root, spec, bind)


def _await_ready(spec: ServiceSpec, handle: Handle, bind: tuple[str, int]) -> None:
    """等待服务就绪并打印下一步指引。"""

    def is_dead() -> bool:
        return not psutil.pid_exists(handle.pid)

    port = wait_ready(spec, handle.pgid, bind[0], timeout=spec.ready_timeout,
                      is_dead=is_dead)

    if port is None:
        if is_dead():
            print_err(f"{spec.label}启动后退出，请查看日志排查：{handle.log_path}")
        else:
            print_warn(
                f"{spec.label}在 {spec.ready_timeout:.0f} 秒内没有就绪。"
                f"进程仍在运行（PID {handle.pid}），日志：{handle.log_path}"
            )
        return

    url_host = normalize_probe_host(bind[0] if spec.id == "backend" else "127.0.0.1")
    url = f"http://{url_host}:{port}"
    guidance = spec.guidance.replace("{url}", url)
    print_ok(f"{spec.label}已就绪（{url}）")
    print_info(guidance)


# --------------------------------------------------------------------------
# 停止与日志
# --------------------------------------------------------------------------


def _stop(root: Path, spec: ServiceSpec, handle: Handle | None) -> None:
    """停止一个由本工具启动的服务。"""
    if handle is None:
        print_warn("没有找到这个服务的进程记录")
        return

    print_info(f"正在停止{spec.label}（PID {handle.pid}）…")
    outcome = stop_group(handle)

    if outcome in (StopOutcome.GRACEFUL, StopOutcome.FORCED):
        clear_handle(pidfile_path(root, spec.id))
        if outcome == StopOutcome.GRACEFUL:
            print_ok(f"{spec.label}已停止")
        else:
            print_ok(f"{spec.label}未响应终止信号，已强制结束")
        # 进程组没了不等于端口一定空了：可能有别的东西立刻占了上去，
        # 如实复查而不是假装成功
        if spec.expected_port is not None:
            owner = port_owner(spec.expected_port)
            if owner is not None:
                print_warn(
                    f"但端口 {spec.expected_port} 仍被 PID {owner[0]}"
                    f"（{owner[1]}）占用"
                )
    elif outcome == StopOutcome.REFUSED_FOREGROUND:
        print_warn("该实例运行在另一个终端的前台，请到那个终端按 Ctrl+C 停止")
    elif outcome == StopOutcome.REFUSED_IDENTITY:
        clear_handle(pidfile_path(root, spec.id))
        print_warn("进程记录与现状对不上（进程可能已退出），记录已清理")
    else:
        print_err(f"{spec.label}未能停止，进程组仍然存在（PID {handle.pid}）")


def _show_logs(root: Path, spec: ServiceSpec) -> None:
    """打印日志末尾。"""
    log_file = logs_dir(root) / f"{spec.id}.log"
    lines = tail_lines(log_file, count=50)
    console.print()
    if not lines:
        print_info(f"还没有日志（{log_file}）。前台运行的服务日志在它们自己的终端里")
    else:
        console.rule(f"{spec.label} · 最近 {len(lines)} 行")
        for line in lines:
            console.print(line, highlight=False, markup=False)
        console.rule()
