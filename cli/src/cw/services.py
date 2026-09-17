"""服务清单与启动命令构造。

这里只放纯数据与纯函数：有哪些服务、各自怎么启动、按什么顺序启动。
所有带副作用的部分（预检、启停、探测）都在别的模块里，由 menu.py 串起来，
所以这个模块可以完全离线地测试。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from cw.errors import CwError
from cw.net import format_url

# 后端接口前缀，与 backend/app/core/config.py 的 API_V1_PREFIX 保持一致
API_V1_PREFIX = "/api/v1"

# frontend/vite.config.ts 的代理目标硬编码为这个端口，见 check_proxy_consistency
BACKEND_DEFAULT_PORT = 8000

# 前端开发服务器的首选端口。vite.config.ts 设了 port 但没开 strictPort，
# 被占用时会自动顺延，所以这只是「期望值」而不是「实际值」
FRONTEND_DEFAULT_PORT = 5173

# 桌面端没有固定端口（electron-vite 不设端口，渲染进程用 vite 默认值），
# 它启动更慢：要先构建 main/preload 再拉起 Electron
DESKTOP_READY_TIMEOUT = 45.0


class Mode(str, Enum):
    """服务的运行方式。"""

    FOREGROUND = "foreground"
    BACKGROUND = "background"


@dataclass(frozen=True)
class ServiceSpec:
    """一个可启动的服务。

    guidance 里可以含 {url} 占位符，由展示层替换成实际地址——
    前端端口可能顺延，所以提示语不能在构造时就把地址写死。
    """

    id: str
    label: str
    cwd: Path
    argv: tuple[str, ...]
    # 期望监听的端口。后端的这是硬约定；前端的可能顺延；桌面端没有
    expected_port: int | None
    # 就绪探测用的健康检查路径，没有则为 None（退化成只探测端口）
    health_path: str | None
    startup_order: int
    ready_timeout: float
    guidance: str


def find_root() -> Path:
    """定位仓库根目录。

    __file__ 是 <root>/cli/src/cw/services.py，向上三级即仓库根。
    这个推导只在 editable 安装下成立（uv sync 对本项目就是 editable），
    所以额外做一次自检，避免被装到别处时抛出一堆与真实原因无关的错误。
    """
    root = Path(__file__).resolve().parents[3]
    if not (root / "backend").is_dir() or not (root / "cli").is_dir():
        raise CwError(
            f"无法定位仓库根目录：{root} 下没有同时找到 backend/ 与 cli/。"
            "cw 需要在源码目录中运行。"
        )
    return root


def backend_python(root: Path) -> Path:
    """后端虚拟环境里的解释器路径。

    直接用它而不是 `uv run uvicorn`：少一层进程、没有每次的环境校验、
    完全离线可用。
    """
    venv = root / "backend" / ".venv"
    windows = venv / "Scripts" / "python.exe"
    if windows.exists():
        return windows
    return venv / "bin" / "python"


def build_services(root: Path, bind: tuple[str, int]) -> dict[str, ServiceSpec]:
    """构造三个服务的定义。bind 是后端实际的 (host, port)。"""
    host, port = bind
    backend_url = format_url(host, port)

    services = [
        ServiceSpec(
            id="backend",
            label="后端 API 服务",
            # cwd 是硬约束：backend/app/core/config.py 里的 DATABASE_URL 和
            # env_file 都是相对路径，在别的目录启动会新建一个空数据库
            cwd=root / "backend",
            # host/port 必须显式传。README 里那条 `uvicorn app.main:app --reload`
            # 用的是 uvicorn 自己的默认值，根本不会读 .env 的 HOST/PORT，
            # 不显式传就会出现「显示 9000、实际监听 8000」
            argv=(
                str(backend_python(root)),
                "-m",
                "uvicorn",
                "app.main:app",
                "--reload",
                "--host",
                host,
                "--port",
                str(port),
            ),
            expected_port=port,
            health_path=f"{API_V1_PREFIX}/health",
            startup_order=10,
            ready_timeout=30.0,
            guidance=f"接口文档 {backend_url}/docs",
        ),
        ServiceSpec(
            id="frontend",
            label="前端 Web 工作台",
            cwd=root / "frontend",
            argv=("npm", "run", "dev"),
            expected_port=FRONTEND_DEFAULT_PORT,
            health_path=None,
            startup_order=20,
            ready_timeout=30.0,
            guidance="浏览器打开 {url}",
        ),
        ServiceSpec(
            id="desktop",
            label="桌面客户端",
            cwd=root / "desktop",
            argv=("npm", "run", "dev"),
            expected_port=None,
            health_path=None,
            startup_order=30,
            ready_timeout=DESKTOP_READY_TIMEOUT,
            guidance="认证信息留在本机，发布任务由它代理执行",
        ),
    ]

    return {spec.id: spec for spec in services}


def full_start_order(specs: Mapping[str, ServiceSpec]) -> list[ServiceSpec]:
    """「全部启动」时的顺序：后端 → 前端 → 桌面端。

    前端和桌面端的开发服务器都会去抢 5173（vite.config.ts 没开 strictPort，
    electron-vite 则完全不设端口，落到 vite 默认值），先启动的赢、后启动的顺延。
    让用户要手动访问的前端拿到约定俗成的 5173，所以前端排在桌面端前面。
    """
    return sorted(specs.values(), key=lambda spec: spec.startup_order)


_PROXY_TARGET_RE = re.compile(r"""target\s*:\s*['"]https?://([^:'"/]+):(\d+)['"]""")


def check_proxy_consistency(root: Path, bind_port: int) -> str | None:
    """检查前端代理目标是否与后端实际端口一致，不一致时返回告警文案。

    frontend/vite.config.ts 里的代理目标是硬编码的。用户把 backend/.env 的
    PORT 改掉之后，前端会静默地全部请求失败——代理打到空端口，浏览器和终端
    都不会给出任何指向原因的线索。这是本仓库里唯一一处「改配置会静默坏掉」
    的地方，所以值得在每次启动时主动验一次。

    读不到配置或匹配不上时返回 None（当作无法判断，而不是报错）。
    """
    config_path = root / "frontend" / "vite.config.ts"
    if not config_path.is_file():
        return None

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return None

    match = _PROXY_TARGET_RE.search(text)
    if match is None:
        return None

    target_host, target_port_text = match.group(1), match.group(2)
    try:
        target_port = int(target_port_text)
    except ValueError:
        return None

    if target_port == bind_port:
        return None

    return (
        f"后端端口为 {bind_port}，但 frontend/vite.config.ts 的代理目标指向 "
        f"{target_host}:{target_port}，前端将无法访问后端。"
        "请同步修改 server.proxy['/api'].target。"
    )
