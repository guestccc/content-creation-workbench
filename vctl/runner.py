"""
子进程执行模块。

统一负责：实时透传输出、日志留痕、异常兜底、退出码传递。

为什么不用 subprocess.run 一次性捕获输出？
因为两个工具都带 tqdm 进度条（尤其是 video-subtitle-remover 处理视频时
会跑很久），必须实时看到进度，否则用户会以为程序卡死。

日志文件落在 工具箱/.vct/logs/ 下，失败时会把路径打出来，方便排查。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import ui
from .env import LOG_DIR

# 每次执行最多保留的日志文件数量，超出后清理最旧的
MAX_LOG_FILES = 200


@dataclass
class RunResult:
    """一次命令执行的结果。"""

    returncode: int
    command: list[str]
    duration: float
    log_path: Path | None = None
    interrupted: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.interrupted


def format_command(command: list[str], cwd: Path | None = None) -> str:
    """把命令列表拼成可直接复制粘贴执行的字符串，用于回显和日志。

    会做 shell 转义，所以复制到终端里就能跑。
    """
    text = shlex.join(str(part) for part in command)
    if cwd is not None:
        text = f"cd {shlex.quote(str(cwd))} && {text}"
    return text


def _prepare_log_path(tag: str) -> Path:
    """生成日志文件路径，并顺带清理过旧的日志。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_tag = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in tag)[:40]
    path = LOG_DIR / f"{stamp}-{safe_tag}.log"

    # 清理旧日志，避免长期使用后目录无限膨胀
    try:
        logs = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime)
        for old in logs[: max(0, len(logs) - MAX_LOG_FILES)]:
            old.unlink(missing_ok=True)
    except OSError:
        # 清理失败不影响主流程
        pass

    return path


def run(
    command: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
    tag: str = "run",
    title: str = "",
    quiet: bool = False,
) -> RunResult:
    """执行一条外部命令，实时把输出透传到终端，同时写入日志。

    Args:
        command: 命令与参数列表（不要在列表里塞 shell 语法）。
        cwd: 工作目录。video-subtitle-remover 必须在自己的根目录下执行。
        env: 额外的环境变量，会叠加在当前环境之上。
        dry_run: 为 True 时只打印命令不执行。
        tag: 日志文件名里的标记，例如 'transcribe'。
        title: 打印在命令前面的中文说明。quiet 为 True 时忽略。
        quiet: 不回显命令行和步骤标题。给批量调用用的 —— 比如按镜头切出
            几十个片段时，每个片段的命令都长得差不多，逐条铺满屏幕只会把
            真正要看的结果冲掉。日志照常写，出问题时仍然查得到。
    Returns:
        RunResult。命令启动失败（比如解释器不存在）时返回 returncode=-1。
    """
    command = [str(part) for part in command]
    command_line = format_command(command, cwd)
    started = time.time()

    if not quiet:
        if title:
            ui.step(title)
        ui.info(ui.dim_text(command_line))

    # ---- dry-run：只回显不执行 ----
    if dry_run:
        ui.warn("这是 dry-run，只打印命令，未真正执行")
        return RunResult(returncode=0, command=command, duration=0.0)

    log_path = _prepare_log_path(tag)

    # ---- 组装环境变量 ----
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    # 让子进程的输出立刻刷出来，不等缓冲区满
    child_env.setdefault("PYTHONUNBUFFERED", "1")

    try:
        with open(log_path, "w", encoding="utf-8") as log_file:
            log_file.write(f"# 命令: {command_line}\n")
            log_file.write(f"# 开始: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            log_file.write("# " + "-" * 60 + "\n")
            log_file.flush()

            process = subprocess.Popen(
                command,
                cwd=str(cwd) if cwd else None,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
    except FileNotFoundError:
        ui.error(f"找不到可执行文件：{command[0]}")
        ui.hint("环境可能已损坏或被移动，运行 `vct doctor` 查看详情。")
        return RunResult(returncode=-1, command=command, duration=time.time() - started)
    except PermissionError:
        ui.error(f"没有执行权限：{command[0]}")
        ui.hint(f"可以尝试：chmod +x {shlex.quote(command[0])}")
        return RunResult(returncode=-1, command=command, duration=time.time() - started)
    except OSError as exc:
        ui.error(f"启动子进程失败：{exc}")
        return RunResult(returncode=-1, command=command, duration=time.time() - started)

    # ---- 实时透传输出 ----
    interrupted = False
    try:
        assert process.stdout is not None
        with open(log_path, "a", encoding="utf-8") as log_file:
            for line in process.stdout:
                # 原样打到终端（不额外缩进，避免破坏进度条对齐）
                sys.stdout.write(line)
                sys.stdout.flush()
                log_file.write(line)
            process.wait()
    except KeyboardInterrupt:
        # 用户按了 Ctrl-C：先礼后兵，让子进程有机会自己收尾
        interrupted = True
        ui.blank()
        ui.warn("收到中断信号，正在停止子进程……")
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            ui.warn("子进程没有响应，强制结束")
            process.kill()
            process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()

    duration = time.time() - started
    returncode = process.returncode if process.returncode is not None else -1

    # 把结束信息补进日志
    try:
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write("\n# " + "-" * 60 + "\n")
            log_file.write(f"# 结束: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            log_file.write(f"# 退出码: {returncode}  耗时: {duration:.1f} 秒\n")
    except OSError:
        pass

    # quiet 模式只压掉「成功」这一句 —— 失败和中断仍然要喊出来，
    # 那正是调用方最需要看见的东西。
    if not quiet or interrupted or returncode != 0:
        ui.blank()
        if interrupted:
            ui.warn(f"执行被中断，耗时 {duration:.1f} 秒")
        elif returncode == 0:
            ui.ok(f"完成，耗时 {duration:.1f} 秒")
        else:
            ui.error(f"执行失败，退出码 {returncode}，耗时 {duration:.1f} 秒")
            ui.hint(f"完整日志：{log_path}")

    return RunResult(
        returncode=returncode,
        command=command,
        duration=duration,
        log_path=log_path,
        interrupted=interrupted,
    )


def run_interactive(
    command: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    tag: str = "run",
    title: str = "",
) -> RunResult:
    """执行一条需要接管终端交互的命令（例如打开图形界面）。

    这里不做输出透传，直接把终端的标准输入输出交给子进程。
    """
    command = [str(part) for part in command]
    if title:
        ui.step(title)
    ui.info(ui.dim_text(format_command(command, cwd)))

    child_env = os.environ.copy()
    if env:
        child_env.update(env)

    try:
        process = subprocess.Popen(command, cwd=str(cwd) if cwd else None, env=child_env)
        process.wait()
    except FileNotFoundError:
        ui.error(f"找不到可执行文件：{command[0]}")
        return RunResult(returncode=-1, command=command, duration=0.0)
    except KeyboardInterrupt:
        ui.blank()
        ui.warn("已中断")
        return RunResult(returncode=130, command=command, duration=0.0, interrupted=True)
    except OSError as exc:
        ui.error(f"启动失败：{exc}")
        return RunResult(returncode=-1, command=command, duration=0.0)

    return RunResult(
        returncode=process.returncode if process.returncode is not None else -1,
        command=command,
        duration=0.0,
    )
