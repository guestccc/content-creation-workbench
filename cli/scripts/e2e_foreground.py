#!/usr/bin/env python3
"""前台模式的端到端验收：Ctrl+C 必须把整组服务一起带走。

这是整个 CLI 设计里最关键的一条安全性质：
前台运行绝不 setsid，服务留在终端的前台进程组里，
用户按 Ctrl+C 时内核把 SIGINT 广播给整组——uvicorn 的 reloader 和 worker
都必须随之退出，端口必须释放。否则下次启动就是 address already in use。
"""

from __future__ import annotations

import os
import pty
import re
import select
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")
DOWN = "\x1b[B"
ENTER = "\r"


def main() -> int:
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(ROOT)
        os.execv(os.path.join(ROOT, "cw"), ["cw"])

    output = ""
    cursor = 0

    def pump() -> None:
        nonlocal output
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return
            output += chunk.decode("utf-8", errors="replace")

    def read_until(needle: str, timeout: float = 30.0) -> bool:
        nonlocal cursor
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in ANSI_RE.sub("", output[cursor:]):
                cursor = len(output)
                return True
            pump()
        return False

    def send(data: str) -> None:
        os.write(fd, data.encode())
        time.sleep(0.4)

    failures = 0

    def check(name: str, ok: bool) -> None:
        nonlocal failures
        print(("✓ " if ok else "✗ ") + name, flush=True)
        if not ok:
            failures += 1

    check("菜单出现", read_until("请选择服务"))
    send(ENTER)                                   # 选后端
    check("子菜单", read_until("后端 API 服务 — 未运行"))
    send(ENTER)                                   # 第一项：前台运行
    check("进入前台运行", read_until("Ctrl+C 停止并返回菜单"))
    check("uvicorn 输出出现", read_until("Uvicorn running", timeout=60))

    time.sleep(1)
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/api/v1/health", timeout=3)
        check("健康检查可访问", True)
    except Exception as exc:
        check(f"健康检查可访问（{exc}）", False)

    # 前台模式的灵魂：Ctrl+C 后整组退出、端口释放、pidfile 清除
    send("\x03")
    check("返回菜单", read_until("请选择服务", timeout=30))

    time.sleep(1)
    port_free = True
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/api/v1/health", timeout=2)
        port_free = False
    except Exception:
        pass
    check("Ctrl+C 后端口 8000 已释放（无孤儿）", port_free)
    check("pidfile 已清除", not os.path.exists(
        os.path.join(ROOT, ".cw", "pids", "backend.json")))

    send("\x03")                                  # 退出 cw
    deadline = time.monotonic() + 10
    code = -1
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            code = os.waitstatus_to_exitcode(status)
            break
        pump()                                    # 必须继续排 pty，见 e2e_pty.py 的教训
    check(f"cw 退出码 130（实际 {code}）", code == 130)

    if failures:
        print(f"\n{failures} 项失败")
        return 1
    print("\n前台模式验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
