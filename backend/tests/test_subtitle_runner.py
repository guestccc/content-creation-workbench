"""字幕提取执行层（subtitle_runner.py）测试。

与 test_scene_runner.py 同一个套路：FakeVcPopen 注入、tick/progress 都设 0，
整个执行流程在主线程同步跑完 —— 不起线程、不起真子进程、不需要真视频。

这里额外钉住两条「字幕功能独有」的判断（实现注释里写明的，改了就坏）：
- 结果以产物为准：退出码 0 但产物缺失判失败；退出码非 0 但产物存在判成功；
- launcher 缺失时整条任务必须落到 failed 终态（修过的 bug：曾经永远停在 running）。
"""

import threading
from pathlib import Path

import pytest

from app.models.subtitle_job import (
    SubtitleJob,
    SubtitleJobItemStatus,
    SubtitleJobStatus,
)
from app.schemas.subtitle_job import SubtitleJobCreate
from app.services.subtitle_env import VcInstall
from app.services.subtitle_job_service import SubtitleJobService
from app.services.subtitle_job_worker import SubtitleJobWorker
from app.services.subtitle_runner import (
    SubtitleRunner,
    build_argv,
    claim_next_pending_id,
    count_segments,
    recover_interrupted_jobs,
    summarize_failure,
)
from tests.conftest import TestingSessionLocal
from tests.fakes import DEFAULT_SRT, FakeVcPopen

FAKE_LAUNCHER = ["/fake/python", "-m", "videocaptioner"]


@pytest.fixture(autouse=True)
def _reset_fake_vc_popen():
    """每个用例开始前清空 FakeVcPopen 实例记录。"""
    FakeVcPopen.reset()
    yield
    FakeVcPopen.reset()


def _make_source(tmp_path, names=("口播A.mp4", "口播B.mp4")) -> Path:
    """造一个含假视频的输入目录。"""
    source = tmp_path / "素材"
    source.mkdir(parents=True, exist_ok=True)
    for name in names:
        (source / name).write_bytes(b"fake-video")
    return source


def _make_job(db_session, tmp_path, names=("口播A.mp4", "口播B.mp4")) -> SubtitleJob:
    """通过服务层创建一个真实任务（含枚举与输出分配）。"""
    source = _make_source(tmp_path, names)
    payload = SubtitleJobCreate(
        input_path=str(source),
        output_dir=str(tmp_path / "字幕"),
        asr="bijian",
        language="zh",
    )
    return SubtitleJobService(db_session).create_job(payload)


def _make_runner(scripts=None, **overrides) -> SubtitleRunner:
    """构造注入 FakeVcPopen 的执行器。

    Args:
        scripts: 逐次调用 popen 时使用的行为脚本列表；None 表示全部默认成功。
        overrides: 透传给 SubtitleRunner 的其他参数（如 video_timeout_seconds），
            会盖掉默认值。
    """
    script_iter = iter(scripts) if scripts is not None else None

    def factory(*args, **kwargs):
        script = next(script_iter) if script_iter is not None else {}
        return FakeVcPopen(*args, script=script, **kwargs)

    # 默认 tick / progress 都设成 0：测试里子进程是假的，没什么可等；
    # launcher 直接注入假前缀，不触发真实探测。
    options = {
        "session_factory": TestingSessionLocal,
        "popen_factory": factory,
        "launcher": FAKE_LAUNCHER,
        "tick_seconds": 0,
        "progress_seconds": 0,
    }
    options.update(overrides)
    return SubtitleRunner(**options)


def _refresh(db_session, job: SubtitleJob) -> SubtitleJob:
    """丢弃会话缓存重新读库（runner 用的是另一条会话）。"""
    db_session.expire_all()
    return db_session.get(SubtitleJob, job.id)


class TestBuildArgv:
    """命令行拼装：白名单、-o 传完整输出文件、可选参数省略。"""

    def test_launcher_prefix_then_transcribe_then_video(self, tmp_path):
        video = tmp_path / "口播.mp4"
        argv = build_argv(FAKE_LAUNCHER, video, tmp_path / "口播.srt", {"asr": "bijian"})
        assert argv[: len(FAKE_LAUNCHER)] == FAKE_LAUNCHER
        assert argv[len(FAKE_LAUNCHER)] == "transcribe"
        assert argv[len(FAKE_LAUNCHER) + 1] == str(video)

    def test_only_whitelisted_params_pass_through(self, tmp_path):
        """params 来自数据库 json 列，白名单以外的键一律不进 argv。"""
        argv = build_argv(
            FAKE_LAUNCHER,
            tmp_path / "a.mp4",
            tmp_path / "a.srt",
            {"asr": "jianying", "language": "zh", "format": "srt", "evil": "--rm"},
        )
        assert argv[argv.index("--asr") + 1] == "jianying"
        assert argv[argv.index("--language") + 1] == "zh"
        assert argv[argv.index("--format") + 1] == "srt"
        assert "evil" not in argv
        assert "--rm" not in argv

    def test_output_is_the_allocated_file_verbatim(self, tmp_path):
        """-o 必须原样是分配好的输出文件（含重名的 -2 后缀）。

        修过的 bug：-o 传目录时 VC 自己按「视频名.srt」起名，跟分配的 -2 路径
        脱节 —— 转写其实成功了、写到了另一个文件，还覆盖了上一份产物，这里却
        按分配路径判「未产出」报失败。带扩展名的路径 VC 会原样使用
        （cli/commands/transcribe.py 的文件模式分支）。
        """
        allocated = tmp_path / "口播-2.srt"
        argv = build_argv(FAKE_LAUNCHER, tmp_path / "口播.mp4", allocated, {})
        assert argv[argv.index("-o") + 1] == str(allocated)

    def test_quiet_always_on(self, tmp_path):
        argv = build_argv(FAKE_LAUNCHER, tmp_path / "a.mp4", tmp_path / "a.srt", {})
        assert "--quiet" in argv

    def test_empty_language_omitted(self, tmp_path):
        """留空即「自动检测」，不能把空串传给 CLI。"""
        argv = build_argv(
            FAKE_LAUNCHER, tmp_path / "a.mp4", tmp_path / "a.srt", {"language": ""}
        )
        assert "--language" not in argv

    def test_default_format_is_srt(self, tmp_path):
        argv = build_argv(FAKE_LAUNCHER, tmp_path / "a.mp4", tmp_path / "a.srt", {})
        assert argv[argv.index("--format") + 1] == "srt"


class TestHelpers:
    """纯函数小工具。"""

    def test_count_segments(self, tmp_path):
        srt = tmp_path / "a.srt"
        srt.write_text(DEFAULT_SRT, encoding="utf-8")
        assert count_segments(srt) == 2

    def test_count_segments_garbage_is_zero(self, tmp_path):
        srt = tmp_path / "a.srt"
        srt.write_text("这不是字幕\n只是一段文字\n", encoding="utf-8")
        assert count_segments(srt) == 0

    def test_count_segments_missing_file_is_zero(self, tmp_path):
        assert count_segments(tmp_path / "不存在.srt") == 0

    def test_summarize_failure_prefers_error_marker(self):
        text = "参数回显一堆\n✗ Error: 音频提取失败\n其他输出"
        assert summarize_failure(text) == "Error: 音频提取失败"

    def test_summarize_failure_falls_back_to_first_line(self):
        assert summarize_failure("第一行\n第二行") == "第一行"
        assert summarize_failure("") == ""


class TestRunJobSuccess:
    """顺利跑完：任务与条目状态、产物统计、日志命名。"""

    def test_success_job(self, db_session, tmp_path):
        job = _make_job(db_session, tmp_path)
        runner = _make_runner()

        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        assert job.status == SubtitleJobStatus.SUCCESS
        assert job.completed_videos == 2
        assert job.failed_videos == 0
        assert job.subtitle_count == 2
        assert job.finished_at is not None
        assert job.child_pid is None
        # 结束后「当前视频」字段要抹掉，否则页面留着一条永远转写中的假进度
        assert job.current_video == ""
        assert job.current_elapsed_seconds == 0

        for item in job.items:
            assert item.status == SubtitleJobItemStatus.SUCCESS
            assert item.exit_code == 0
            assert item.subtitle_exists is True
            assert item.segment_count == 2
            assert item.file_size > 0
            assert Path(item.output_path).is_file()
            # 日志与字幕同目录、同名加 .videocaptioner.log（分隔符不能丢）
            log_path = Path(item.output_path).parent / (
                f"{Path(item.output_path).stem}.videocaptioner.log"
            )
            assert log_path.is_file()

    def test_argv_reaches_subprocess(self, db_session, tmp_path):
        """起子进程的 argv：launcher 前缀 + transcribe + 视频 + -o 分配路径。"""
        job = _make_job(db_session, tmp_path)
        _make_runner().run_job(job.id)

        assert len(FakeVcPopen.instances) == 2
        argv = FakeVcPopen.instances[0].argv
        assert argv[: len(FAKE_LAUNCHER)] == FAKE_LAUNCHER
        assert argv[len(FAKE_LAUNCHER)] == "transcribe"
        # -o 必须是分配好的那份输出文件（而不是目录），与 item 落库的路径一致
        assert argv[argv.index("-o") + 1] == job.items[0].output_path
        assert argv[argv.index("--asr") + 1] == "bijian"
        assert argv[argv.index("--language") + 1] == "zh"


class TestResultJudgement:
    """结果以产物为准（模块头部说明 b），退出码只用于失败归因。"""

    def test_exit_zero_without_output_is_failure(self, db_session, tmp_path):
        """退出码 0 但没产出 .srt（比如识别出 0 条语音）：判失败。"""
        job = _make_job(db_session, tmp_path, names=("无声.mp4",))
        runner = _make_runner(
            scripts=[{"no_output": True, "log_lines": ["✗ Error: 没有识别到语音内容"]}]
        )
        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        item = job.items[0]
        assert item.status == SubtitleJobItemStatus.FAILED
        assert item.exit_code == 0
        assert item.subtitle_exists is False
        assert "没有识别到语音内容" in item.error_message
        assert job.status == SubtitleJobStatus.FAILED
        assert job.subtitle_count == 0

    def test_nonzero_exit_with_output_is_success(self, db_session, tmp_path):
        """收尾阶段报错但字幕已写好：认成功，原因记下来给用户看。"""
        job = _make_job(db_session, tmp_path, names=("口播.mp4",))
        runner = _make_runner(
            scripts=[{"exit_code": 5, "log_lines": ["✗ Error: 保存日志失败"]}]
        )
        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        item = job.items[0]
        assert item.status == SubtitleJobItemStatus.SUCCESS
        assert item.exit_code == 5
        assert item.subtitle_exists is True
        assert "字幕已生成，但命令以退出码 5 结束" in item.error_message
        assert job.status == SubtitleJobStatus.SUCCESS
        assert job.subtitle_count == 1

    def test_empty_output_file_is_failure(self, db_session, tmp_path):
        """产出了 0 字节文件：与没产出同等对待。"""
        job = _make_job(db_session, tmp_path, names=("空.mp4",))
        runner = _make_runner(scripts=[{"empty_output": True}])
        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        item = job.items[0]
        assert item.status == SubtitleJobItemStatus.FAILED
        assert item.subtitle_exists is False
        assert "未产出字幕文件" in item.error_message

    def test_exit_zero_no_output_does_not_show_bare_path(self, db_session, tmp_path):
        """退出码 0、日志只有一行输出路径（--quiet 的正常输出）：不能拿路径当失败原因。

        复现的真实事故：VC 把字幕写到了自己起的名字里（-o 传目录时的行为），
        分配的路径上没有产物，日志里只有它打印的结果路径一行 —— 界面上展示的
        「失败原因」就是那行路径，用户完全看不懂发生了什么。
        """
        job = _make_job(db_session, tmp_path, names=("无声.mp4",))
        allocated = Path(job.items[0].output_path)
        runner = _make_runner(
            scripts=[{"no_output": True, "log_lines": [str(allocated)]}]
        )
        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        item = job.items[0]
        assert item.status == SubtitleJobItemStatus.FAILED
        # 第一行是能看懂的结论，路径只在后面的日志原文里出现
        first_line = item.error_message.splitlines()[0]
        assert "未产出字幕文件" in first_line
        assert str(allocated) not in first_line

    def test_partial_when_some_fail(self, db_session, tmp_path):
        """一批里成败各半：任务 partial，字幕数只算成功的。"""
        job = _make_job(db_session, tmp_path)
        runner = _make_runner(scripts=[{"no_output": True}, {}])
        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        assert job.items[0].status == SubtitleJobItemStatus.FAILED
        assert job.items[1].status == SubtitleJobItemStatus.SUCCESS
        assert job.status == SubtitleJobStatus.PARTIAL
        assert job.failed_videos == 1
        assert job.subtitle_count == 1
        # 任务级失败摘要带来源文件名，用户不用逐条点开看
        assert "口播A.mp4" in job.error_message


class TestCancel:
    """取消语义：当前条与后续标 skipped，已产出的字幕保留。"""

    def test_cancel_during_run_skips_remaining(self, db_session, tmp_path):
        job = _make_job(db_session, tmp_path)
        service = SubtitleJobService(db_session)

        def cancelling_factory(*args, **kwargs):
            """子进程启动后立刻取消任务，模拟执行中点了取消。"""
            instance = FakeVcPopen(*args, script={"hang": True}, **kwargs)
            service.cancel_job(job.id)
            return instance

        runner = _make_runner(popen_factory=cancelling_factory)
        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        assert job.status == SubtitleJobStatus.CANCELLED
        assert all(
            item.status == SubtitleJobItemStatus.SKIPPED for item in job.items
        )
        assert job.items[0].error_message == "任务已取消"
        assert job.skipped_videos == 2
        assert job.child_pid is None
        assert job.finished_at is not None

    def test_cancel_keeps_produced_subtitle(self, db_session, tmp_path):
        """取消时字幕已经写好了：如实告知「已保留」，文件留在磁盘上。"""
        job = _make_job(db_session, tmp_path)
        service = SubtitleJobService(db_session)

        produced = Path(job.items[0].output_path)
        produced.write_text(DEFAULT_SRT, encoding="utf-8")

        def cancelling_factory(*args, **kwargs):
            instance = FakeVcPopen(
                *args, script={"hang": True, "no_output": True}, **kwargs
            )
            service.cancel_job(job.id)
            return instance

        runner = _make_runner(popen_factory=cancelling_factory)
        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        assert job.status == SubtitleJobStatus.CANCELLED
        first, second = job.items
        assert first.status == SubtitleJobItemStatus.SKIPPED
        assert first.error_message == "任务已取消，但字幕已生成（已保留在输出目录）"
        assert second.status == SubtitleJobItemStatus.SKIPPED
        assert second.error_message == "任务已取消"
        # 产物是用户的，取消不能顺手删文件
        assert produced.read_text(encoding="utf-8") == DEFAULT_SRT

    def test_cancelled_job_is_not_claimed(self, db_session, tmp_path):
        """非 pending 任务干净地返回 False，一个子进程都不起。"""
        job = _make_job(db_session, tmp_path)
        SubtitleJobService(db_session).cancel_job(job.id)

        runner = _make_runner()
        assert runner.run_job(job.id) is False
        assert FakeVcPopen.instances == []


class TestTimeoutAndStop:
    """单条硬超时与服务停止信号。"""

    def test_video_timeout_marks_item_failed(self, db_session, tmp_path):
        """卡住的转写不能堵死队列：超时杀进程组，继续下一条。"""
        job = _make_job(db_session, tmp_path)
        runner = _make_runner(
            scripts=[{"hang": True}, {}],
            video_timeout_seconds=0,
            stop_grace_seconds=0.1,
        )
        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        first, second = job.items
        assert first.status == SubtitleJobItemStatus.FAILED
        assert "转写超时" in first.error_message
        assert second.status == SubtitleJobItemStatus.SUCCESS
        assert job.status == SubtitleJobStatus.PARTIAL

    def test_stop_event_before_start_skips_all(self, db_session, tmp_path):
        """lifespan 已请求停止：未执行的条目标 skipped，任务 failed。"""
        job = _make_job(db_session, tmp_path)
        stop = threading.Event()
        stop.set()

        runner = _make_runner()
        assert runner.run_job(job.id, stop_event=stop) is True

        job = _refresh(db_session, job)
        assert all(
            item.status == SubtitleJobItemStatus.SKIPPED for item in job.items
        )
        # 服务停止不能写成「任务已取消」—— 用户会以为谁点了取消
        assert job.items[0].error_message == "服务停止或重启，未执行"
        assert job.status == SubtitleJobStatus.FAILED
        assert job.skipped_videos == 2
        assert FakeVcPopen.instances == []

    def test_stop_during_run_fails_current_item(self, db_session, tmp_path):
        """跑到一半收到停止信号：当前条算 failed（不是 skipped），后续 skipped。"""
        job = _make_job(db_session, tmp_path)
        stop = threading.Event()

        scripts = [
            {"hang": True, "on_poll": lambda _proc, _n: stop.set()},
            {},
        ]
        runner = _make_runner(scripts=scripts, stop_grace_seconds=0.1)
        assert runner.run_job(job.id, stop_event=stop) is True

        job = _refresh(db_session, job)
        first, second = job.items
        assert first.status == SubtitleJobItemStatus.FAILED
        assert "服务停止" in first.error_message
        assert "可直接重新发起" in first.error_message
        assert second.status == SubtitleJobItemStatus.SKIPPED
        assert second.error_message == "服务停止或重启，未执行"
        assert job.status == SubtitleJobStatus.FAILED


class TestLauncherMissing:
    """排队期间 VideoCaptioner 被卸载：整条任务必须落到 failed 终态。

    这是修过的 bug：_fail_all 只标了条目，任务自己的状态没落，会永远停在
    running。这里钉住回归。
    """

    def test_missing_launcher_fails_whole_job(self, db_session, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.services.subtitle_runner.detect",
            lambda *a, **k: VcInstall(installed=False, detail="没找到"),
        )
        job = _make_job(db_session, tmp_path)

        # 不传 launcher：走 detect() 分支（刚被改成未安装）
        runner = SubtitleRunner(
            session_factory=TestingSessionLocal,
            popen_factory=FakeVcPopen,
            tick_seconds=0,
            progress_seconds=0,
        )
        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        assert job.status == SubtitleJobStatus.FAILED
        assert job.finished_at is not None
        assert "未探测到可用的 VideoCaptioner" in job.error_message
        assert all(
            item.status == SubtitleJobItemStatus.FAILED for item in job.items
        )
        assert FakeVcPopen.instances == []


class TestClaimAndRecover:
    """队列认领与启动恢复。"""

    def test_run_twice_second_returns_false(self, db_session, tmp_path):
        """终态任务不能再被认领执行。"""
        job = _make_job(db_session, tmp_path)
        runner = _make_runner()
        assert runner.run_job(job.id) is True
        assert runner.run_job(job.id) is False

    def test_claim_next_pending_id_picks_earliest(self, db_session, tmp_path):
        first = _make_job(db_session, tmp_path / "一", names=("a.mp4",))
        second = _make_job(db_session, tmp_path / "二", names=("b.mp4",))
        assert claim_next_pending_id(session_factory=TestingSessionLocal) == first.id

        SubtitleJobService(db_session).cancel_job(first.id)
        assert claim_next_pending_id(session_factory=TestingSessionLocal) == second.id

    def test_recover_interrupted_jobs(self, db_session, tmp_path):
        """启动回收：running 任务 → failed；running 条目 → failed；pending → skipped。"""
        job = _make_job(db_session, tmp_path)
        job.status = SubtitleJobStatus.RUNNING
        job.child_pid = None  # 上次退出时没留下子进程句柄
        job.items[0].status = SubtitleJobItemStatus.RUNNING
        db_session.commit()

        recovered = recover_interrupted_jobs(session_factory=TestingSessionLocal)
        assert recovered == 1

        job = _refresh(db_session, job)
        assert job.status == SubtitleJobStatus.FAILED
        assert job.error_message == "服务重启，任务中断"
        assert job.items[0].status == SubtitleJobItemStatus.FAILED
        assert job.items[0].error_message == "服务重启，任务中断"
        assert job.items[1].status == SubtitleJobItemStatus.SKIPPED

    def test_recover_ignores_pending_jobs(self, db_session, tmp_path):
        """pending 任务不需要恢复：DB 队列会自动接手。"""
        _make_job(db_session, tmp_path)
        assert recover_interrupted_jobs(session_factory=TestingSessionLocal) == 0


class TestWorker:
    """工作线程（全仓库字幕侧唯一真起线程的用例，完全不碰 DB）。"""

    def test_worker_drains_queue_then_idles(self):
        """按顺序执行认领到的任务，队列空后空转，stop 能立即停下。"""
        executed = []
        queue = iter([11, 22])

        class FakeRunner:
            def run_job(self, job_id, stop_event=None):
                executed.append(job_id)
                return True

        worker = SubtitleJobWorker(
            claim_next=lambda: next(queue, None),
            runner_factory=lambda: FakeRunner(),
            poll_seconds=0.01,
        )
        worker.start()
        try:
            # 两个任务应很快被依次执行完：轮询等待而不是干等固定时长
            for _ in range(200):
                if executed == [11, 22]:
                    break
                threading.Event().wait(0.01)
            assert executed == [11, 22]
        finally:
            worker.stop(grace=2)
