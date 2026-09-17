"""服务就绪探测。

为什么不能只看「进程活着」就算启动成功：
  - 后端 uvicorn 从进程拉起到真正监听端口之间有一段空白期；
  - 前端 vite 在 5173 被占用时会静默顺延到 5174——只看期望端口会误判；
  - 桌面端 Electron 根本没有端口，只能看进程组活着 + 日志出现就绪标志。

所以这里提供两种探测：端口监听探测（顺便发现顺延后的实际端口），
以及后端的 HTTP 健康检查。健康检查用 stdlib 的 urllib，
不为这点事引入 httpx。
"""

from __future__ import annotations

import json
from collections.abc import Callable
import urllib.error
import urllib.request

from cw.net import normalize_probe_host
from cw.process import group_listen_ports
from cw.services import ServiceSpec

# 每次探测的间隔与 HTTP 超时。太短会空转刷 CPU，太长会让「启动失败」的反馈变慢
_PROBE_INTERVAL = 0.3
_HTTP_TIMEOUT = 1.0


def wait_for_port(
    spec: ServiceSpec,
    pgid: int,
    timeout: float,
    is_dead: Callable[[], bool] | None = None,
) -> int | None:
    """等待进程组开始监听端口，返回实际端口。

    优先级：期望端口 > 进程组实际监听的任意端口。
    后者兜住 vite 端口顺延的情况——5173 被占时 vite 会去 5174，
    死等 5173 只会误报启动失败。

    is_dead 是一个可选的「进程已退出」回调，传入后进程一旦退出就立刻
    放弃等待，而不是傻等到超时。
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ports = group_listen_ports(pgid)
        if ports:
            if spec.expected_port is not None and spec.expected_port in ports:
                return spec.expected_port
            return ports[0]
        if is_dead is not None and is_dead():
            return None
        time.sleep(_PROBE_INTERVAL)
    return None


def check_health(host: str, port: int, path: str) -> bool:
    """对后端健康检查端点发一次 HTTP GET，2xx 视为健康。

    所有网络异常（连接拒绝、超时、重置）都按「还没就绪」处理，
    这是探测语义而不是错误语义。
    """
    url = f"http://{normalize_probe_host(host)}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError):
        return False


def wait_ready(
    spec: ServiceSpec,
    pgid: int,
    bind_host: str,
    timeout: float,
    is_dead: Callable[[], bool] | None = None,
) -> int | None:
    """等待服务真正就绪：先等端口监听，有健康检查路径再过一道 HTTP。

    返回实际端口号；超时或进程退出返回 None。
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        port = wait_for_port(spec, pgid, timeout=min(1.0, max(0.0, deadline - time.monotonic())),
                             is_dead=is_dead)
        if port is None:
            return None
        if spec.health_path is None:
            return port
        if check_health(bind_host, port, spec.health_path):
            return port
        if is_dead is not None and is_dead():
            return None
        time.sleep(_PROBE_INTERVAL)
    return None


def log_signals_ready(log_text: str, markers: tuple[str, ...]) -> bool:
    """日志里是否出现了就绪标志。

    给没有端口可探的服务用（桌面端 Electron）。
    """
    return any(marker in log_text for marker in markers)


def parse_vite_url(log_text: str) -> str | None:
    """从 vite 的输出里提取实际监听的 URL，作为端口探测的交叉验证。

    vite 就绪时会打印 `➜  Local:   http://localhost:5173/`。
    """
    import re

    match = re.search(r"Local:\s+(http://\S+)", log_text)
    return match.group(1) if match else None


def json_dumps_safe(data: object) -> str:
    """调试用的小工具：把探测结果安全地转成字符串。"""
    try:
        return json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(data)
