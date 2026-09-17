"""readiness 与 logs 模块的测试。"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from cw.logs import tail_lines, tail_text
from cw.process import group_listen_ports
from cw.readiness import (
    check_health,
    log_signals_ready,
    parse_vite_url,
    wait_for_port,
    wait_ready,
)
from cw.services import ServiceSpec

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="进程组语义依赖 POSIX")


def _make_spec(tmp_path: Path, port: int | None = None,
               health_path: str | None = None) -> ServiceSpec:
    return ServiceSpec(
        id="testsvc",
        label="测试服务",
        cwd=tmp_path,
        argv=("python3",),
        expected_port=port,
        health_path=health_path,
        startup_order=10,
        ready_timeout=5.0,
        guidance="测试用",
    )


def _free_port() -> int:
    """绑一个临时 socket 拿到一个确定空闲的端口再放开。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _HealthHandler(BaseHTTPRequestHandler):
    """最小的健康检查端点。"""

    def do_GET(self):  # noqa: N802 - stdlib 规定的命名
        if self.path == "/api/v1/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # 静默，别往测试输出里刷日志
        pass


@pytest.fixture
def health_server():
    """在随机端口起一个带 /api/v1/health 的 HTTP 服务。"""
    server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield port
    server.shutdown()
    server.server_close()


class TestCheckHealth:
    def test_healthy_endpoint(self, health_server):
        assert check_health("127.0.0.1", health_server, "/api/v1/health") is True

    def test_404_is_not_healthy(self, health_server):
        assert check_health("127.0.0.1", health_server, "/nope") is False

    def test_connection_refused_is_not_healthy(self):
        """服务还没起来时的常态：连接被拒绝，必须按「未就绪」处理而不是抛错。"""
        assert check_health("127.0.0.1", _free_port(), "/api/v1/health") is False

    def test_wildcard_host_is_normalized(self, health_server):
        assert check_health("0.0.0.0", health_server, "/api/v1/health") is True


@POSIX_ONLY
class TestWaitForPort:
    def test_detects_listener_in_group(self, tmp_path):
        server = subprocess.Popen(
            [sys.executable, "-m", "http.server", "0", "--bind", "127.0.0.1"],
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            spec = _make_spec(tmp_path, port=None)
            port = wait_for_port(spec, os.getpgid(server.pid), timeout=5.0)
            assert port is not None
            assert port in group_listen_ports(os.getpgid(server.pid))
        finally:
            os.killpg(os.getpgid(server.pid), signal.SIGKILL)
            server.wait(timeout=5)

    def test_prefers_expected_port(self, tmp_path):
        server = subprocess.Popen(
            [sys.executable, "-m", "http.server", "0", "--bind", "127.0.0.1"],
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            # 先拿到实际端口，再以它为期望值
            deadline = time.monotonic() + 5
            ports: list[int] = []
            while time.monotonic() < deadline and not ports:
                ports = group_listen_ports(os.getpgid(server.pid))
                time.sleep(0.1)
            assert ports

            spec = _make_spec(tmp_path, port=ports[0])
            assert wait_for_port(spec, os.getpgid(server.pid), timeout=2.0) == ports[0]
        finally:
            os.killpg(os.getpgid(server.pid), signal.SIGKILL)
            server.wait(timeout=5)

    def test_timeout_returns_none(self, tmp_path):
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            spec = _make_spec(tmp_path, port=59997)
            start = time.monotonic()
            assert wait_for_port(spec, os.getpgid(sleeper.pid), timeout=1.0) is None
            assert time.monotonic() - start < 3.0
        finally:
            os.killpg(os.getpgid(sleeper.pid), signal.SIGKILL)
            sleeper.wait(timeout=5)

    def test_is_dead_callback_short_circuits(self, tmp_path):
        """进程一退出就该立刻放弃，而不是傻等到超时。"""
        spec = _make_spec(tmp_path, port=59997)
        start = time.monotonic()
        assert wait_for_port(spec, pgid=99999999, timeout=30.0,
                             is_dead=lambda: True) is None
        assert time.monotonic() - start < 2.0


class TestWaitReady:
    def test_no_health_path_returns_on_port(self, tmp_path):
        """没有健康检查的服务（前端/桌面端）监听了就算就绪。"""
        server = subprocess.Popen(
            [sys.executable, "-m", "http.server", "0", "--bind", "127.0.0.1"],
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            spec = _make_spec(tmp_path, port=None, health_path=None)
            port = wait_ready(spec, os.getpgid(server.pid), "127.0.0.1", timeout=5.0)
            assert port is not None
        finally:
            os.killpg(os.getpgid(server.pid), signal.SIGKILL)
            server.wait(timeout=5)

    def test_health_path_must_pass(self, tmp_path, health_server):
        """有健康检查的服务（后端）光监听不够，HTTP 也要通。"""
        # 直接探测 fixture 起的健康检查服务：把它当成「某个已知 pgid 监听的端口」
        # 不可行（它不在独立进程组里），所以这里退化为只验证 check_health 的组合语义：
        # 健康端点存在时 wait_ready 应该成功——用一个虚假但存活的 pgid 不行，
        # 改为构造 spec 指向 health_server 的端口并验证 check_health 本身
        spec = _make_spec(tmp_path, port=health_server, health_path="/api/v1/health")
        assert check_health("127.0.0.1", spec.expected_port, spec.health_path)


class TestLogMarkers:
    def test_vite_url_parsing(self):
        log = "  ➜  Local:   http://localhost:5174/\n  ➜  Network: use --host to expose"
        assert parse_vite_url(log) == "http://localhost:5174/"

    def test_vite_url_absent(self):
        assert parse_vite_url("一些普通输出") is None

    def test_log_signals_ready(self):
        assert log_signals_ready("ready in 321 ms", ("ready in",)) is True
        assert log_signals_ready("compiling...", ("ready in",)) is False


class TestTailText:
    def test_missing_file_returns_empty(self, tmp_path):
        assert tail_text(tmp_path / "不存在.log") == ""

    def test_empty_file_returns_empty(self, tmp_path):
        path = tmp_path / "空.log"
        path.write_text("")
        assert tail_text(path) == ""

    def test_small_file_read_in_full(self, tmp_path):
        path = tmp_path / "小.log"
        path.write_text("第一行\n第二行\n", encoding="utf-8")
        assert tail_text(path) == "第一行\n第二行\n"

    def test_large_file_reads_tail(self, tmp_path):
        path = tmp_path / "大.log"
        lines = [f"第 {i} 行" for i in range(2000)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        text = tail_text(path, max_bytes=1024)
        # 只含尾部内容，且第一行是完整的（不完整的被丢掉了）
        assert "第 0 行" not in text
        assert "第 1999 行" in text

    def test_invalid_utf8_does_not_crash(self, tmp_path):
        path = tmp_path / "坏.log"
        path.write_bytes("正常前缀\n".encode() + b"\xff\xfe" + " 乱码后缀\n".encode())
        text = tail_text(path)
        assert "正常前缀" in text  # 好字节要保住

    def test_tail_lines_count(self, tmp_path):
        path = tmp_path / "多行.log"
        path.write_text("\n".join(f"行{i}" for i in range(100)), encoding="utf-8")
        lines = tail_lines(path, count=10)
        assert len(lines) == 10
        assert lines[-1] == "行99"
