#!/usr/bin/env python3
"""用伪终端驱动 ./cw 做一次端到端验收：

  1. 进入菜单（第一项是后端，直接回车）
  2. 选择「后台运行并返回」（下移一次 + 回车）
  3. 等就绪后从外部访问健康检查
  4. 回到菜单停止服务（运行中的动作列表：查看日志 / 停止服务 / 返回）
  5. 验证端口释放、pidfile 清除
  6. 从子菜单返回主菜单，Ctrl+C 退出

不是 pytest 用例——它需要真实的仓库环境和交互终端，
放在 scripts/ 下手动运行。

注意 read_until 是游标式匹配：每次成功匹配后游标前移，
不会把上一次菜单渲染里的旧文本当成新事件（菜单文本会重复出现）。
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import sys
import time
import urllib.request

# __file__ 是 <root>/cli/scripts/e2e_pty.py，向上三级才是仓库根
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")

DOWN = "\x1b[B"
ENTER = "\r"


class PtyDriver:
    def __init__(self, argv: list[str], cwd: str):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(cwd)
            os.execv(argv[0], argv)
        self.output = ""
        # 已消费的输出位置。匹配只搜游标之后的内容
        self.cursor = 0

    def _pump(self) -> bool:
        ready, _, _ = select.select([self.fd], [], [], 0.2)
        if not ready:
            return True
        try:
            chunk = os.read(self.fd, 65536)
        except OSError:
            return False
        if not chunk:
            return False
        self.output += chunk.decode("utf-8", errors="replace")
        return True

    def read_until(self, needle: str, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            plain = ANSI_RE.sub("", self.output[self.cursor:])
            if needle in plain:
                self.cursor = len(self.output)
                return True
            if not self._pump():
                return False
        return False

    def send(self, data: str, settle: float = 0.4) -> None:
        os.write(self.fd, data.encode())
        time.sleep(settle)

    def wait_exit(self, timeout: float = 10.0) -> int:
        # 等待期间必须继续读 pty：子进程退出时如果缓冲区没被排空，
        # 内核会让它卡在 ttywait 里永远无法变成僵尸，waitpid 也就永不返回
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                return os.waitstatus_to_exitcode(status)
            self._pump()
        os.kill(self.pid, 9)
        # kill 之后同样要一边排一边等
        while True:
            done, _ = os.waitpid(self.pid, os.WNOHANG)
            if done:
                return -1
            self._pump()


def main() -> int:
    driver = PtyDriver([os.path.join(ROOT, "cw")], cwd=ROOT)
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print(("✓ " if ok else "✗ ") + name, flush=True)
        if not ok:
            failures.append(name)
            tail = ANSI_RE.sub("", driver.output)[-1200:]
            print(f"    --- 子进程输出尾部 ---\n{tail}\n    ---", flush=True)

    # 1. 菜单出现
    check("菜单出现", driver.read_until("请选择服务"))

    # 2. 选中后端（第一项），回车
    driver.send(ENTER)
    check("进入后端子菜单", driver.read_until("后端 API 服务 — 未运行"))

    # 3. 下移到「后台运行并返回」，回车
    driver.send(DOWN + ENTER)
    check("后端后台启动就绪", driver.read_until("已就绪", timeout=60))

    # 4. 外部验证健康检查与 pidfile
    time.sleep(0.5)
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/health", timeout=3) as resp:
            body = json.loads(resp.read())
        check("健康检查返回 success", body.get("success") is True)
    except Exception as exc:
        check(f"健康检查可访问（{exc}）", False)

    pidfile = os.path.join(ROOT, ".cw", "pids", "backend.json")
    check("pidfile 存在", os.path.isfile(pidfile))
    if os.path.isfile(pidfile):
        with open(pidfile) as f:
            handle = json.load(f)
        check("pidfile 含 create_time 指纹", "create_time" in handle)
        check("pidfile 标记后台模式", handle.get("mode") == "background")
        check("pidfile 记录日志路径", bool(handle.get("log_path")))

    # 5. 后台运行后回到主菜单，再进后端子菜单
    check("返回主菜单", driver.read_until("请选择服务", timeout=15))
    driver.send(ENTER)
    check("子菜单显示运行中", driver.read_until("运行中，PID"))

    # 6. 动作列表是 查看日志 / 停止服务 / 返回，下移一次到「停止服务」
    driver.send(DOWN + ENTER)
    check("停止成功", driver.read_until("已停止", timeout=20))

    # 7. 端口释放 + pidfile 清除
    time.sleep(0.5)
    port_free = True
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/api/v1/health", timeout=2)
        port_free = False
    except Exception:
        pass
    check("端口 8000 已释放", port_free)
    check("pidfile 已清除", not os.path.exists(pidfile))

    # 8. 停止后仍在子菜单（动作变回 前台/后台/返回），下移两次选「返回」
    check("子菜单回到未运行", driver.read_until("未运行", timeout=15))
    driver.send(DOWN + DOWN + ENTER)
    check("回到主菜单", driver.read_until("请选择服务", timeout=15))

    # 9. Ctrl+C 退出
    driver.send("\x03", settle=0.1)
    code = driver.wait_exit()
    check(f"Ctrl+C 退出码为 130（实际 {code}）", code == 130)

    if failures:
        print(f"\n{len(failures)} 项失败")
        return 1
    print("\n端到端验收全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
