"""media_tools 模块的回归测试。

这些函数是从 scene_runner.py 平移过来的，本文件钉住「平移后行为不变」：
PATH 注入、进程组终止对非法 PID 的容错、命令行探测的边界。

另外钉住两条踩过坑的规则：子进程输出必须自己解码（decode_output /
run_probe），以及「找到同名文件 ≠ 它就是这个工具」（verify_tool）。
"""

import sys

import pytest

from app.services.media_tools import (
    child_env,
    command_line,
    decode_output,
    find_tool,
    run_probe,
    terminate_process_group,
    verify_tool,
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


class TestDecodeOutput:
    """子进程输出的解码：任何字节都不许抛异常（这条是崩过任务的）。"""

    def test_utf8_chinese_round_trips(self):
        """ffprobe 的 JSON 会原样回显素材路径，中文必须是中文。"""
        assert decode_output("自行车.mp4".encode("utf-8")) == "自行车.mp4"

    def test_empty_input(self):
        assert decode_output(None) == ""
        assert decode_output(b"") == ""

    def test_undecodable_bytes_do_not_raise(self):
        """坏字节退化成替换字符，绝不抛 UnicodeDecodeError。

        中文 Windows 的本地代码页是 GBK，ffprobe 吐的是 UTF-8：两者混在一起
        时 text=True 会在 subprocess 的读线程里抛异常，stdout 变成 None。
        """
        assert isinstance(decode_output(b"\xaa\xbb\xff\xfe"), str)


class TestRunProbe:
    """只读探测命令：捕获输出、超时/起不来都降级成 None。"""

    def test_captures_stdout_as_text(self):
        result = run_probe([sys.executable, "-c", "print('自行车 720x1280')"])
        assert result is not None
        assert result.returncode == 0
        assert "自行车 720x1280" in result.stdout

    def test_missing_executable_returns_none(self, tmp_path):
        """起不来（文件不存在）不抛异常，返回 None —— 探测失败不是崩溃。"""
        assert run_probe([str(tmp_path / "没有这个程序")]) is None

    def test_timeout_returns_none(self):
        result = run_probe(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3
        )
        assert result is None


class TestVerifyTool:
    """工具自证：同名文件不一定是它自己，冒充的必须被识破。"""

    def test_none_or_missing_path_is_rejected(self, tmp_path):
        assert verify_tool("ffprobe", None) == ""
        assert verify_tool("ffprobe", "") == ""
        assert verify_tool("ffprobe", str(tmp_path / "并没有这个程序")) == ""

    def test_wrong_tool_is_rejected(self):
        """拿 python 冒充 ffprobe：跑得起来，但自报名号对不上 → 不认。"""
        assert verify_tool("ffprobe", sys.executable) == ""

    @pytest.mark.skipif(
        find_tool("ffprobe") is None, reason="本机没有 ffprobe"
    )
    def test_real_ffprobe_self_reports(self):
        """PATH 里的 ffprobe 必须真的是 ffprobe，且报得出真版本号。

        这条用例正是那个坑的回归守卫：曾有一个 ffmpeg.exe 的副本被命名成
        ffprobe.exe 放在 ~/.local/bin，名字探测全绿，混剪任务却读不出素材规格。
        """
        path = find_tool("ffprobe")
        version = verify_tool("ffprobe", path)
        assert version, f"{path} 自称不是 ffprobe（实际输出：{run_probe([path, '-version']).stdout.splitlines()[:1]}）"
        assert version[0].isdigit()
