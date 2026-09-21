"""media_tools 模块的回归测试。

这些函数是从 scene_runner.py 平移过来的，本文件钉住「平移后行为不变」：
PATH 注入、进程组终止对非法 PID 的容错、命令行探测的边界。

另外钉住两条踩过坑的规则：子进程输出必须自己解码（decode_output /
run_probe），以及「找到同名文件 ≠ 它就是这个工具」（verify_tool）。
后一条还要钉住**失败的分档**：「跑不起来」和「自称是别的工具」给用户的
修复建议完全不同，混成一档会把换架构的人带去查文件名（见 PROBE_* 常量）。
"""

import errno
import sys

import pytest

from app.services import media_tools
from app.services.media_tools import (
    PROBE_BADARCH,
    PROBE_EACCES,
    PROBE_IMPOSTOR,
    PROBE_MISSING,
    PROBE_OK,
    child_env,
    command_line,
    decode_output,
    dependency_status,
    find_tool,
    run_probe,
    terminate_process_group,
    verify_tool,
    verify_tool_detail,
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


class TestVerifyToolDetail:
    """自检失败的分档：跑不起来 ≠ 冒充，修复动作不一样。"""

    def test_empty_path_is_missing(self):
        assert verify_tool_detail("ffprobe", None)[1] == PROBE_MISSING
        assert verify_tool_detail("ffprobe", "")[1] == PROBE_MISSING

    def test_impostor_is_flagged_as_impostor(self, monkeypatch):
        """跑得起来、正常退出，只是自报的名号不是它 —— 这才是「冒充」。

        打桩而不用真的冒充者：失败档位取决于子进程的退出码，而任何解释器对
        `-version` 的反应都不稳定，不如把「跑起来了」这件事直接钉死。
        """
        monkeypatch.setattr(
            media_tools,
            "spawn_probe",
            lambda argv, **kwargs: media_tools.SpawnResult(
                media_tools.ProbedOutput(0, "ffmpeg version 6.1.1 Copyright", ""),
                PROBE_OK,
                "",
            ),
        )
        version, code, message = verify_tool_detail("ffprobe", "/fake/ffprobe")
        assert version == ""
        assert code == PROBE_IMPOSTOR
        assert message == "ffmpeg version"  # 冒充者报出的名号要留下来，日志里好用

    def test_success_reports_ok_and_version(self, monkeypatch):
        """名号对得上：解析出首行第三个词当版本号。"""
        monkeypatch.setattr(
            media_tools,
            "spawn_probe",
            lambda argv, **kwargs: media_tools.SpawnResult(
                media_tools.ProbedOutput(0, "ffprobe version 6.1.1 Copyright", ""),
                PROBE_OK,
                "",
            ),
        )
        version, code, _ = verify_tool_detail("ffprobe", "/fake/ffprobe")
        assert version == "6.1.1"
        assert code == PROBE_OK

    def test_unexecutable_file_is_not_reported_as_impostor(self, tmp_path):
        """跑不起来的文件绝不能被判成「冒充别的工具」。

        这是 macOS 上的真实场景：系统停掉 Rosetta 后，PATH 里那份 ffmpeg
        文件名没错、内容也没错，只是架构跑不了。旧实现把它一律报成
        「可能是别的工具的副本被改名」，用户照着去查文件名，永远修不好。
        """
        path = tmp_path / "ffprobe"
        path.write_text("这不是一个可执行文件\n")
        path.chmod(0o755)
        version, code, _ = verify_tool_detail("ffprobe", str(path))
        assert version == ""
        assert code != PROBE_IMPOSTOR


class TestDescribeOsError:
    """起不来时的 OSError 要说成人话 —— 尤其别把架构问题说成「文件不存在」。"""

    def test_badarch_is_named(self):
        # EBADARCH 只在 macOS 上存在，别的平台跳过（错误码对不上就没意义）
        if not hasattr(errno, "EBADARCH"):
            pytest.skip("本平台没有 EBADARCH")
        message = media_tools.describe_os_error(OSError(errno.EBADARCH, "Bad CPU type"))
        assert "架构" in message

    def test_eacces_is_named(self):
        message = media_tools.describe_os_error(OSError(errno.EACCES, "Permission denied"))
        assert "权限" in message

    def test_unknown_errno_falls_back_to_strerror(self):
        assert media_tools.describe_os_error(OSError(12345, "怪错误")) == "怪错误"


class TestDependencyStatus:
    """依赖自检的结果契约：同一份 dependencies[] 给两个页面用，两档不能串。"""

    def _status(self, monkeypatch, code, message=""):
        monkeypatch.setattr(media_tools, "find_tool", lambda name: "/fake/ffmpeg")
        monkeypatch.setattr(
            media_tools,
            "verify_tool_detail",
            lambda name, path: ("", code, message),
        )
        return dependency_status(
            "ffmpeg",
            purpose="视频切割与缩略图抽帧",
            missing_detail="未在 PATH 中找到",
            missing_hint="装一个 ffmpeg",
            impostor_detail="找到的同名文件不是 ffmpeg（可能是别的工具的副本被改名）",
            impostor_hint="装一份完整的 ffmpeg",
        )

    def test_ready_when_verified(self, monkeypatch):
        monkeypatch.setattr(media_tools, "find_tool", lambda name: "/fake/ffmpeg")
        monkeypatch.setattr(
            media_tools, "verify_tool_detail", lambda name, path: ("6.1.1", PROBE_OK, "")
        )
        status = dependency_status(
            "ffmpeg", purpose="视频切割", missing_detail="未找到", missing_hint="装一个"
        )
        assert status["ok"] is True
        assert status["detail"] == "视频切割"
        assert status["fix_hint"] == ""

    def test_badarch_says_unexecutable_and_hints_arch(self, monkeypatch):
        """架构不匹配：说「无法执行」并指向换架构，不许说成「被改名」。"""
        status = self._status(
            monkeypatch, PROBE_BADARCH, "系统不支持这个文件的架构（Bad CPU type）"
        )
        assert status["ok"] is False
        assert "无法执行" in status["detail"]
        assert "Bad CPU type" in status["detail"]
        assert "改名" not in status["detail"]
        assert "arm64" in status["fix_hint"]

    def test_eacces_hints_permission(self, monkeypatch):
        status = self._status(monkeypatch, PROBE_EACCES, "没有执行权限")
        assert "权限" in status["fix_hint"]

    def test_impostor_keeps_caller_copy(self, monkeypatch):
        """真冒充时仍用调用方给的文案（它更清楚该怎么修）。"""
        status = self._status(monkeypatch, PROBE_IMPOSTOR, "sfx version 1.0")
        assert "改名" in status["detail"]

    def test_missing_still_uses_missing_copy(self, monkeypatch):
        monkeypatch.setattr(media_tools, "find_tool", lambda name: None)
        status = dependency_status(
            "ffmpeg",
            purpose="视频切割",
            missing_detail="未在 PATH 中找到",
            missing_hint="装一个 ffmpeg",
        )
        assert status["path"] == ""
        assert status["detail"] == "未在 PATH 中找到"
        assert status["fix_hint"] == "装一个 ffmpeg"
