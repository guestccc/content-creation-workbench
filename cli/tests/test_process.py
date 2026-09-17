"""process 模块的测试。

进程启停是整个 CLI 风险最高的部分：测错了会杀掉测试进程自己所在的
进程组。因此本文件里所有真正发信号的用例都遵守同一条铁律——
**被测进程必须是 setsid 过的独立进程组**，绝不对共享组的对象调用
stop_group / _signal_group。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from cw.errors import ProcessError
from cw.process import (
    Handle,
    Status,
    StopOutcome,
    clear_handle,
    determine_state,
    ensure_dirs,
    group_listen_ports,
    group_members,
    is_alive,
    pidfile_path,
    port_owner,
    read_handle,
    spawn_background,
    spawn_foreground,
    stop_group,
    wait_foreground,
    write_handle,
)
from cw.services import Mode, ServiceSpec

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="进程组语义依赖 POSIX")


def _make_spec(tmp_path: Path, argv: tuple[str, ...], port: int | None = None) -> ServiceSpec:
    """构造一个最小可用的 ServiceSpec。"""
    return ServiceSpec(
        id="testsvc",
        label="测试服务",
        cwd=tmp_path,
        argv=argv,
        expected_port=port,
        health_path=None,
        startup_order=10,
        ready_timeout=5.0,
        guidance="测试用",
    )


def _make_handle(**overrides) -> Handle:
    """造一个字段齐全的 Handle，按需覆盖。"""
    defaults = dict(
        service_id="testsvc",
        pid=os.getpid(),
        pgid=os.getpgid(0),
        create_time=psutil.Process(os.getpid()).create_time(),
        mode=Mode.BACKGROUND.value,
        cmd=("python3", "-c", "pass"),
        cwd="/tmp",
        log_path=None,
        started_at="2026-09-17T10:00:00+08:00",
    )
    defaults.update(overrides)
    return Handle(**defaults)


def _start_listener(cwd: Path) -> tuple[int, subprocess.Popen]:
    """起一个监听随机端口的 http.server，返回 (实际端口, 进程)。

    注意不能靠读 stdout 拿端口：stdout 接到管道时 http.server 的输出
    是块缓冲的，readline 会一直阻塞。用进程组的监听端口反查。
    """
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", "0", "--bind", "127.0.0.1"],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    ports: list[int] = []
    while time.monotonic() < deadline and not ports:
        ports = group_listen_ports(os.getpgid(server.pid))
        time.sleep(0.1)
    if not ports:
        server.kill()
        server.wait(timeout=5)
        raise AssertionError("http.server 没有监听任何端口")
    return ports[0], server


@pytest.fixture
def process_tree(tmp_path):
    """起一个「父进程派生子进程后一起 sleep」的独立进程组。

    setsid 是关键：没有这个，stop_group 会把 pytest 自己也杀掉。
    用例结束后无论成败都兜底清理。
    """
    if os.name != "posix":
        pytest.skip("进程组语义依赖 POSIX")
    child_pid_file = tmp_path / "child.pid"
    program = (
        "import os, subprocess, sys, time;"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"open({str(child_pid_file)!r}, 'w').write(str(child.pid));"
        "time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    # 等子进程派生出来
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not child_pid_file.exists():
        time.sleep(0.05)

    pgid = os.getpgid(proc.pid)
    handle = _make_handle(pid=proc.pid, pgid=pgid,
                          create_time=psutil.Process(proc.pid).create_time())

    yield handle, pgid

    # 兜底清理：组里还有活口就强杀
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait(timeout=5)


class TestPidfile:
    def test_roundtrip(self, tmp_path):
        path = pidfile_path(tmp_path, "backend")
        handle = _make_handle(service_id="backend", pid=1234, pgid=1234,
                              create_time=1700000000.5, cmd=("a", "b"),
                              log_path="/tmp/x.log")
        write_handle(path, handle)
        loaded = read_handle(path)
        assert loaded == handle

    def test_write_is_atomic_no_tmp_left(self, tmp_path):
        path = pidfile_path(tmp_path, "backend")
        write_handle(path, _make_handle())
        assert not path.with_name(path.name + ".tmp").exists()

    def test_read_missing_returns_none(self, tmp_path):
        assert read_handle(pidfile_path(tmp_path, "ghost")) is None

    def test_read_corrupt_json_returns_none(self, tmp_path):
        path = pidfile_path(tmp_path, "backend")
        path.parent.mkdir(parents=True)
        path.write_text("{不是合法 JSON", encoding="utf-8")
        assert read_handle(path) is None

    def test_read_missing_fields_returns_none(self, tmp_path):
        path = pidfile_path(tmp_path, "backend")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"service_id": "backend"}), encoding="utf-8")
        assert read_handle(path) is None

    def test_clear_missing_is_silent(self, tmp_path):
        clear_handle(pidfile_path(tmp_path, "ghost"))  # 不应抛错

    def test_clear_removes_file(self, tmp_path):
        path = pidfile_path(tmp_path, "backend")
        write_handle(path, _make_handle())
        clear_handle(path)
        assert read_handle(path) is None


class TestEnsureDirs:
    def test_creates_dirs(self, tmp_path):
        ensure_dirs(tmp_path)
        assert (tmp_path / ".cw" / "pids").is_dir()
        assert (tmp_path / ".cw" / "logs").is_dir()
        # 幂等
        ensure_dirs(tmp_path)

    def test_file_in_the_way_raises(self, tmp_path):
        blocker = tmp_path / ".cw"
        blocker.write_text("我是一个文件")
        with pytest.raises(ProcessError, match="同名文件"):
            ensure_dirs(tmp_path)


class TestIsAlive:
    def test_current_process_is_alive(self):
        assert is_alive(_make_handle()) is True

    def test_wrong_create_time_is_dead(self):
        """PID 复用的防护：create_time 对不上就视为不是同一个进程。"""
        handle = _make_handle(create_time=1.0)
        assert is_alive(handle) is False

    def test_wrong_pgid_is_dead(self):
        handle = _make_handle(pgid=os.getpgid(0) + 999999)
        assert is_alive(handle) is False

    def test_nonexistent_pid_is_dead(self):
        handle = _make_handle(pid=99999999)
        assert is_alive(handle) is False


@POSIX_ONLY
class TestProcessGroup:
    def test_group_members_includes_children(self, process_tree):
        """npm/uvicorn 类服务都是进程树，组内必须能看到父子两个进程。"""
        _, pgid = process_tree
        members = group_members(pgid)
        assert len(members) >= 2

    def test_is_alive_on_real_handle(self, process_tree):
        handle, _ = process_tree
        assert is_alive(handle) is True

    def test_group_listen_ports_empty_for_sleepers(self, process_tree):
        _, pgid = process_tree
        assert group_listen_ports(pgid) == []


@POSIX_ONLY
class TestSpawnBackground:
    def test_spawns_and_records_handle(self, tmp_path):
        spec = _make_spec(tmp_path, (sys.executable, "-c", "import time; time.sleep(30)"))
        log_file = tmp_path / ".cw" / "logs" / "testsvc.log"
        handle = spawn_background(spec, log_file)
        try:
            assert handle.pid == handle.pgid  # setsid 后组长是自己
            assert handle.mode == Mode.BACKGROUND.value
            assert handle.log_path == str(log_file)
            assert is_alive(handle)
        finally:
            os.killpg(handle.pgid, signal.SIGKILL)

    def test_immediate_exit_raises(self, tmp_path):
        spec = _make_spec(tmp_path, (sys.executable, "-c", "raise SystemExit(1)"))
        # 立刻退出的进程，create_time 查询可能抢在退出前——所以只断言
        # 「要么抛 ProcessError，要么句柄已经死了」
        log_file = tmp_path / ".cw" / "logs" / "x.log"
        try:
            handle = spawn_background(spec, log_file)
        except ProcessError:
            return
        time.sleep(0.3)
        assert not is_alive(handle)

    def test_missing_executable_raises(self, tmp_path):
        spec = _make_spec(tmp_path, ("/不存在的/解释器",))
        with pytest.raises(ProcessError, match="启动测试服务失败"):
            spawn_background(spec, tmp_path / "x.log")


@POSIX_ONLY
class TestStopGroup:
    def test_refuses_foreground_handle(self, tmp_path):
        """前台实例的生命周期归终端管，跨终端停止必须被拒绝。"""
        handle = _make_handle(mode=Mode.FOREGROUND.value)
        assert stop_group(handle) == StopOutcome.REFUSED_FOREGROUND

    def test_refuses_pgid_mismatch(self, tmp_path):
        handle = _make_handle(pgid=os.getpgid(0) + 1)
        assert stop_group(handle) == StopOutcome.REFUSED_IDENTITY

    def test_refuses_dead_process(self, tmp_path):
        handle = _make_handle(pid=99999999, pgid=99999999)
        assert stop_group(handle) == StopOutcome.REFUSED_IDENTITY

    def test_refuses_reused_pid(self, tmp_path):
        """create_time 对不上 = PID 被复用了，必须拒绝而不是乱杀。"""
        handle = _make_handle(create_time=1.0)
        assert stop_group(handle) == StopOutcome.REFUSED_IDENTITY

    def test_stops_entire_tree(self, process_tree):
        """核心安全用例：整组（父+子）都要被清掉，不留孤儿。"""
        handle, pgid = process_tree
        outcome = stop_group(handle, term_timeout=5.0, kill_timeout=2.0)
        assert outcome in (StopOutcome.GRACEFUL, StopOutcome.FORCED)
        assert group_members(pgid) == []


@POSIX_ONLY
class TestWaitForeground:
    def test_returns_exit_code(self, tmp_path):
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(3)"])
        assert wait_foreground(proc) == 3


@POSIX_ONLY
class TestPortOwner:
    def test_detects_listener(self, tmp_path):
        """起一个真正的监听 socket，确认能找到占用者。"""
        port, server = _start_listener(tmp_path)
        try:
            owner = port_owner(port)
            assert owner is not None
            owner_pid, desc = owner
            assert owner_pid == server.pid
            assert desc  # 描述非空即可
        finally:
            os.killpg(os.getpgid(server.pid), signal.SIGKILL)
            server.wait(timeout=5)

    def test_free_port_returns_none(self):
        # 绑一个临时 socket 拿一个确定空闲的端口再放开
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]
        sock.close()
        assert port_owner(free_port) is None


@POSIX_ONLY
class TestDetermineState:
    def test_stopped_when_nothing(self, tmp_path):
        spec = _make_spec(tmp_path, ("python3",), port=59998)
        state = determine_state(spec, tmp_path)
        assert state.status == Status.STOPPED

    def test_stale_when_process_gone(self, tmp_path):
        spec = _make_spec(tmp_path, ("python3",), port=59998)
        write_handle(pidfile_path(tmp_path, "testsvc"),
                     _make_handle(pid=99999999, pgid=99999999))
        state = determine_state(spec, tmp_path)
        assert state.status == Status.STALE
        assert state.handle is not None

    def test_running_owned_with_alive_handle(self, tmp_path):
        spec = _make_spec(tmp_path, (sys.executable, "-c", "import time; time.sleep(30)"))
        handle = spawn_background(spec, tmp_path / "x.log")
        write_handle(pidfile_path(tmp_path, "testsvc"), handle)
        try:
            state = determine_state(spec, tmp_path)
            assert state.status == Status.RUNNING_OWNED
            assert state.handle is not None
        finally:
            stop_group(handle, term_timeout=3.0, kill_timeout=2.0)

    def test_running_foreign_when_cwd_matches(self, tmp_path):
        """进程不是我们起的，但工作目录一致 → 视为「外部启动的同一服务」。"""
        port, server = _start_listener(tmp_path)
        try:
            spec = _make_spec(tmp_path, ("python3",), port=port)
            state = determine_state(spec, tmp_path)
            assert state.status == Status.RUNNING_FOREIGN
            assert state.port == port
        finally:
            os.killpg(os.getpgid(server.pid), signal.SIGKILL)
            server.wait(timeout=5)

    def test_port_taken_when_cwd_differs(self, tmp_path):
        """端口被别的目录下的进程占着 → PORT_TAKEN，绝不冒认为自己的服务。"""
        other_dir = tmp_path / "别处"
        other_dir.mkdir()
        port, server = _start_listener(other_dir)
        try:
            spec = _make_spec(tmp_path, ("python3",), port=port)
            state = determine_state(spec, tmp_path)
            assert state.status == Status.PORT_TAKEN
            assert "占用" in state.detail
        finally:
            os.killpg(os.getpgid(server.pid), signal.SIGKILL)
            server.wait(timeout=5)
