"""media_tools 模块的回归测试。

这些函数是从 scene_runner.py 平移过来的，本文件钉住「平移后行为不变」：
PATH 注入、进程组终止对非法 PID 的容错、命令行探测的边界。
"""

from app.services.media_tools import (
    child_env,
    command_line,
    find_tool,
    terminate_process_group,
)


def test_child_env_prepends_local_bin():
    """child_env 必须把 ~/.local/bin 前插到 PATH（uvicorn 从图形界面启动时 PATH 很贫瘠）。"""
    env = child_env()
    assert env["PATH"].split(":")[0].endswith(".local/bin")


def test_find_tool_finds_sh():
    """sh 一定存在，用来验证 find_tool 的查找链路本身是通的。"""
    assert find_tool("sh") is not None


def test_find_tool_returns_none_for_missing():
    assert find_tool("definitely-not-a-real-tool-xyz") is None


def test_terminate_process_group_ignores_invalid_pid():
    """非法 PID 直接返回，不抛异常（清理路径绝不能炸主流程）。"""
    terminate_process_group(0, 0.1)
    terminate_process_group(-1, 0.1)


def test_terminate_process_group_ignores_dead_pid():
    """杀一个不存在的大 PID：ProcessLookupError 必须被吞掉。"""
    terminate_process_group(4_000_000, 0.1)


def test_command_line_rejects_invalid_pid():
    assert command_line(0) == ""
    assert command_line(-5) == ""


def test_command_line_reads_current_process():
    """能读到当前进程的命令行，说明 ps 调用链路正常。"""
    import os

    assert command_line(os.getpid()) != ""
