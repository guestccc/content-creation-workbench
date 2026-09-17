"""服务进程的启停、pidfile 与状态判定。

这个模块承担了本工具最大的风险面：操作系统里 PID 会被复用，
而我们的停止动作是向「整个进程组」发信号。一旦身份校验缺失或判断错误，
就不是「服务没停掉」这种小问题，而是会杀掉无关进程。因此下面所有
涉及信号发送的函数都会先做身份校验，宁可拒绝也不误杀。

进程组是这里的核心概念：
  - `npm run dev` 会派生 node → vite，`uvicorn --reload` 会派生 reloader → worker；
  - 只终止直接子进程，会留下孤儿继续占着端口，表现为「明明停了却起不来」；
  - 所以后台启动时让服务自成一个进程组（start_new_session=True），停止时整组终止。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import psutil

from cw.errors import ProcessError
from cw.services import Mode, ServiceSpec

# 运行时数据目录，位于仓库根目录下，已被 .gitignore 忽略
CW_DIR_NAME = ".cw"

# create_time 的浮点比较容差。JSON 往返本身是无损的，
# 留一点余量只是为了不依赖平台时钟精度
_CREATE_TIME_TOLERANCE = 0.001

# 轮询进程组是否清空时的间隔
_POLL_INTERVAL = 0.1


class Status(str, Enum):
    """一个服务当前处于什么状态。"""

    # 由本工具启动，进程组仍然存活
    RUNNING_OWNED = "running_owned"
    # 端口在监听，但不是本工具启动的（用户自己开的终端）
    RUNNING_FOREIGN = "running_foreign"
    # 端口被一个与我们的服务无关的程序占用
    PORT_TAKEN = "port_taken"
    # 没有运行
    STOPPED = "stopped"
    # 有 pidfile 但对应的进程已经没了
    STALE = "stale"


class StopOutcome(str, Enum):
    """一次停止操作的结果。"""

    # 收到 SIGTERM 后自行退出
    GRACEFUL = "graceful"
    # 宽限期内没退，被 SIGKILL 强杀
    FORCED = "forced"
    # 前台运行中，终端才是它的归属，不接受从别处停止
    REFUSED_FOREGROUND = "refused_foreground"
    # 身份校验没通过，出于安全拒绝发送信号
    REFUSED_IDENTITY = "refused_identity"
    # 信号发出去了但进程组仍在
    STILL_ALIVE = "still_alive"


@dataclass
class Handle:
    """一次服务启动的完整记录，落盘在 .cw/pids/<service>.json。

    create_time 是关键：它和 PID 一起构成进程身份。系统复用 PID 后
    create_time 必然不同，靠它才能确认「这个 PID 还是当初那个进程」。
    """

    service_id: str
    pid: int
    pgid: int
    create_time: float
    mode: str
    cmd: tuple[str, ...]
    cwd: str
    log_path: str | None
    started_at: str


@dataclass
class ServiceState:
    """一个服务在某一时刻的完整状态，供界面直接渲染。"""

    spec: ServiceSpec
    status: Status
    handle: Handle | None = None
    # 实际监听到的端口。后端的应与 expected_port 相同；
    # 前端可能因为 5173 被占而顺延，所以以这里为准
    port: int | None = None
    detail: str = ""


# --------------------------------------------------------------------------
# 目录与文件
# --------------------------------------------------------------------------


def cw_dir(root: Path) -> Path:
    return root / CW_DIR_NAME


def pids_dir(root: Path) -> Path:
    return cw_dir(root) / "pids"


def logs_dir(root: Path) -> Path:
    return cw_dir(root) / "logs"


def pidfile_path(root: Path, service_id: str) -> Path:
    return pids_dir(root) / f"{service_id}.json"


def ensure_dirs(root: Path) -> None:
    """创建运行时目录，幂等。

    顺带处理「同名路径已被一个普通文件占住」的情况——这种错误在别处
    只会表现为诡异的 FileNotFoundError，不如在这里说清楚。
    """
    for directory in (pids_dir(root), logs_dir(root)):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except (FileExistsError, NotADirectoryError) as exc:
            # 目标或路径上的某一级被普通文件占住了：
            # FileExistsError 是目标本身，NotADirectoryError 是中间某一级
            raise ProcessError(
                f"运行时目录被一个同名文件挡住了：{directory}。请先移除它。"
            ) from exc
        except OSError as exc:
            raise ProcessError(f"无法创建运行时目录 {directory}：{exc}") from exc


def write_handle(path: Path, handle: Handle) -> None:
    """原子写入 pidfile。

    先写临时文件再 rename：CLI 有可能在写入过程中被 Ctrl+C 打断，
    直接写会让下次读到半截 JSON。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(asdict(handle), ensure_ascii=False, indent=2)
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise ProcessError(f"无法写入进程记录 {path}：{exc}") from exc


def read_handle(path: Path) -> Handle | None:
    """读取 pidfile，文件不存在或内容损坏时返回 None。

    内容损坏时返回 None 而不是抛错：一个坏掉的记录不该让整个菜单打不开，
    它会在下次启动时被覆盖掉。
    """
    if not path.is_file():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    try:
        return Handle(
            service_id=str(data["service_id"]),
            pid=int(data["pid"]),
            pgid=int(data["pgid"]),
            create_time=float(data["create_time"]),
            mode=str(data["mode"]),
            cmd=tuple(data.get("cmd") or ()),
            cwd=str(data.get("cwd") or ""),
            log_path=data.get("log_path"),
            started_at=str(data.get("started_at") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def clear_handle(path: Path) -> None:
    """删除 pidfile，不存在时静默返回。"""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # 删不掉不要紧，下次启动会覆盖
        pass


# --------------------------------------------------------------------------
# 进程组
# --------------------------------------------------------------------------


def group_members(pgid: int) -> list[psutil.Process]:
    """枚举进程组内的所有进程。

    macOS 的 `ps -g` 是「进程组组长」的语义而不是「组内成员」，
    `lsof` 也不能按进程组过滤，所以只能这样逐个比对。

    僵尸进程会被排除：它们已经退出、只是还没被回收，
    算进来会让「组是否清空」永远为假。
    """
    members: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "status"]):
        try:
            if proc.info.get("status") == psutil.STATUS_ZOMBIE:
                continue
            if os.getpgid(proc.info["pid"]) == pgid:
                members.append(proc)
        except (ProcessLookupError, PermissionError, OSError, KeyError):
            # 进程在遍历过程中退出是常态，跳过即可
            continue
    return members


def group_listen_ports(pgid: int) -> list[int]:
    """进程组内所有进程正在监听的 TCP 端口。

    这是判断「服务到底起在哪个端口」最可靠的方式：前端和桌面端的开发服务器
    在 5173 被占用时会自动顺延，硬编码的期望值并不可信。
    """
    ports: set[int] = set()
    for proc in group_members(pgid):
        try:
            connections = proc.net_connections(kind="tcp")
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
        for conn in connections:
            if conn.status == psutil.CONN_LISTEN and conn.laddr:
                ports.add(int(conn.laddr.port))
    return sorted(ports)


def port_owner(port: int) -> tuple[int, str] | None:
    """查出正在监听指定端口的进程，返回 (pid, 描述)。没人监听则返回 None。"""
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            connections = proc.net_connections(kind="tcp")
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
        for conn in connections:
            if (
                conn.status == psutil.CONN_LISTEN
                and conn.laddr
                and int(conn.laddr.port) == port
            ):
                return int(proc.info["pid"]), _describe_process(proc)
    return None


def _describe_process(proc: psutil.Process) -> str:
    """给出一句能让人认出来的进程描述。"""
    try:
        argv = proc.cmdline()
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        argv = []
    if argv:
        # 只取前几段，完整命令行往往很长且带一堆参数
        return " ".join(argv[:4])
    try:
        return proc.name()
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return "未知进程"


# --------------------------------------------------------------------------
# 身份校验
# --------------------------------------------------------------------------


def is_alive(handle: Handle) -> bool:
    """handle 记录的那个进程是否还活着。

    两道校验缺一不可：
      1. create_time 必须与记录一致——PID 被复用后必然不同，
         这是区分「我们的服务」和「恰好占用了同一个 PID 的无关进程」的唯一依据；
      2. 进程组必须还是记录的那个——否则说明进程已经换组，killpg 会打错目标。
    """
    try:
        proc = psutil.Process(handle.pid)
        actual_create_time = proc.create_time()
        actual_pgid = os.getpgid(handle.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ProcessLookupError):
        return False

    if abs(actual_create_time - handle.create_time) > _CREATE_TIME_TOLERANCE:
        return False

    return actual_pgid == handle.pgid


def _belongs_to_service(pid: int, spec: ServiceSpec) -> bool:
    """判断某个进程是不是这个服务的实例。

    依据是工作目录：同一台机器上可能有别人开的 uvicorn / vite，
    但工作目录落在本仓库对应子目录里的，基本可以确定是同一个服务。
    """
    try:
        cwd = psutil.Process(pid).cwd()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False

    try:
        return Path(cwd).resolve() == spec.cwd.resolve()
    except OSError:
        return False


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------


def spawn_background(spec: ServiceSpec, log_file: Path) -> Handle:
    """在后台启动服务，输出重定向到日志文件。

    start_new_session=True 让服务自成一个进程组，这是后台模式能被整组
    终止的前提。
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        log_handle = log_file.open("ab")
    except OSError as exc:
        raise ProcessError(f"无法打开日志文件 {log_file}：{exc}") from exc

    try:
        proc = subprocess.Popen(
            spec.argv,
            cwd=spec.cwd,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise ProcessError(f"启动{spec.label}失败：{exc}") from exc
    finally:
        # 子进程已经持有自己的文件描述符副本，父进程这份可以关掉了
        log_handle.close()

    try:
        pgid = os.getpgid(proc.pid)
        create_time = psutil.Process(proc.pid).create_time()
    except (ProcessLookupError, OSError, psutil.NoSuchProcess) as exc:
        # 启动后立刻退出，通常是依赖没装或命令不存在
        raise ProcessError(
            f"{spec.label}启动后立即退出，请查看日志：{log_file}"
        ) from exc

    return Handle(
        service_id=spec.id,
        pid=proc.pid,
        pgid=pgid,
        create_time=create_time,
        mode=Mode.BACKGROUND.value,
        cmd=tuple(spec.argv),
        cwd=str(spec.cwd),
        log_path=str(log_file),
        started_at=_utc_now_iso(),
    )


def spawn_foreground(spec: ServiceSpec) -> tuple[Handle, subprocess.Popen]:
    """在前台启动服务，返回句柄与进程对象，由调用方等待其结束。

    这里绝不能传 start_new_session=True。子进程一旦 setsid 就脱离了终端的
    前台进程组，用户按 Ctrl+C 时内核只通知前台进程组——结果是 CLI 退出了，
    uvicorn / vite 变成孤儿继续占着端口，下次启动报 address already in use。
    """
    try:
        proc = subprocess.Popen(spec.argv, cwd=spec.cwd)
    except OSError as exc:
        raise ProcessError(f"启动{spec.label}失败：{exc}") from exc

    try:
        pgid = os.getpgid(proc.pid)
        create_time = psutil.Process(proc.pid).create_time()
    except (ProcessLookupError, OSError, psutil.NoSuchProcess) as exc:
        raise ProcessError(f"{spec.label}启动后立即退出，请检查依赖是否已安装") from exc

    handle = Handle(
        service_id=spec.id,
        pid=proc.pid,
        pgid=pgid,
        create_time=create_time,
        mode=Mode.FOREGROUND.value,
        cmd=tuple(spec.argv),
        cwd=str(spec.cwd),
        log_path=None,
        started_at=_utc_now_iso(),
    )
    return handle, proc


def wait_foreground(proc: subprocess.Popen) -> int:
    """等待前台进程结束，返回退出码。

    收到 Ctrl+C 时终端已经把 SIGINT 广播给了整个前台进程组，服务正在
    自己收尾（uvicorn 还要跑 engine.dispose()），所以给一段宽限期，
    而不是立刻强杀。
    """
    try:
        return proc.wait()
    except KeyboardInterrupt:
        try:
            return proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _signal_group(proc.pid, signal.SIGKILL)
            return proc.wait()


# --------------------------------------------------------------------------
# 停止
# --------------------------------------------------------------------------


def stop_group(
    handle: Handle,
    term_timeout: float = 10.0,
    kill_timeout: float = 3.0,
) -> StopOutcome:
    """终止服务所在的整个进程组。"""
    if handle.mode == Mode.FOREGROUND.value:
        # 前台实例挂在别的终端的进程组里，它的生命周期归那边的 Ctrl+C 管
        return StopOutcome.REFUSED_FOREGROUND

    # 三重身份校验。任何一条不满足都拒绝——裸 PID 会被复用，
    # 不做校验的 killpg 等同于拿一个数字去杀任意进程组
    if handle.pgid != handle.pid:
        # start_new_session=True 时组长必然就是它自己，不等说明记录不可信
        return StopOutcome.REFUSED_IDENTITY
    if not is_alive(handle):
        return StopOutcome.REFUSED_IDENTITY
    try:
        if os.getpgid(handle.pid) != handle.pgid:
            return StopOutcome.REFUSED_IDENTITY
    except (ProcessLookupError, PermissionError, OSError):
        return StopOutcome.REFUSED_IDENTITY

    _signal_group(handle.pgid, signal.SIGTERM)
    if _wait_group_gone(handle.pgid, term_timeout):
        return StopOutcome.GRACEFUL

    _signal_group(handle.pgid, signal.SIGKILL)
    if _wait_group_gone(handle.pgid, kill_timeout):
        return StopOutcome.FORCED

    return StopOutcome.STILL_ALIVE


def _signal_group(pid_or_pgid: int, sig: int) -> None:
    """向进程组发信号。

    组长先退出后组可能已不存在，这种情况直接忽略；权限不足则如实报告。
    """
    try:
        os.killpg(pid_or_pgid, sig)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise ProcessError(f"没有权限终止进程组 {pid_or_pgid}") from exc


def _wait_group_gone(pgid: int, timeout: float) -> bool:
    """等待进程组清空。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not group_members(pgid):
            return True
        time.sleep(_POLL_INTERVAL)
    return not group_members(pgid)


# --------------------------------------------------------------------------
# 状态判定
# --------------------------------------------------------------------------


def determine_state(spec: ServiceSpec, root: Path) -> ServiceState:
    """综合 pidfile 与端口两个信息源，得出服务当前状态。

    两个信息源回答的是不同问题，缺一不可：
      - pidfile 说明「这是不是本工具启动的、还活着吗」；
      - 端口说明「是不是真的有东西在监听」。
    只信 pidfile 会看不见用户在别的终端里自己起的服务；
    只信端口则会把「恰好占用了 8000 的无关程序」当成服务在运行。
    """
    handle = read_handle(pidfile_path(root, spec.id))

    if handle is not None:
        if is_alive(handle):
            ports = group_listen_ports(handle.pgid)
            return ServiceState(
                spec=spec,
                status=Status.RUNNING_OWNED,
                handle=handle,
                port=_pick_port(ports, spec.expected_port),
            )
        # 记录还在但进程没了，留待用户清理
        return ServiceState(
            spec=spec,
            status=Status.STALE,
            handle=handle,
            detail="进程已退出，留下了一条记录",
        )

    if spec.expected_port is not None:
        owner = port_owner(spec.expected_port)
        if owner is not None:
            owner_pid, owner_desc = owner
            if _belongs_to_service(owner_pid, spec):
                return ServiceState(
                    spec=spec,
                    status=Status.RUNNING_FOREIGN,
                    port=spec.expected_port,
                    detail="由本工具以外的进程启动",
                )
            return ServiceState(
                spec=spec,
                status=Status.PORT_TAKEN,
                port=spec.expected_port,
                detail=f"端口被 PID {owner_pid}（{owner_desc}）占用",
            )

    return ServiceState(spec=spec, status=Status.STOPPED)


def _pick_port(ports: list[int], expected: int | None) -> int | None:
    """从进程组实际监听的端口里挑一个用于展示。

    优先返回期望端口；前端 5173 被占用时会顺延，这时返回实际的那个。
    """
    if not ports:
        return None
    if expected is not None and expected in ports:
        return expected
    return ports[0]


def _utc_now_iso() -> str:
    """带时区偏移的 ISO8601 时间戳。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
