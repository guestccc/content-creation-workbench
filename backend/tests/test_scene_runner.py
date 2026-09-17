"""镜头分割执行层测试：SceneRunner + FakePopen 在主线程同步跑完。

覆盖的正是真实环境里最容错的几种情况：
1. 单镜头视频：退出码 0 但零片段 —— 合法结果，不能显示成失败；
2. 部分片段失败：退出码 5 但已有片段产出 —— 不能当成全败；
3. 批量中单条失败不中断后续视频；
4. 取消、超时、服务停止三条中断路径；
5. 启动时回收上次异常退出的 running 任务。
"""

import threading
from pathlib import Path

import pytest

from app.models.scene_job import (
    SceneJob,
    SceneJobItemStatus,
    SceneJobMode,
    SceneJobStatus,
)
from app.schemas.scene_job import SceneJobCreate
from app.services.scene_job_service import SceneJobService
from app.services.scene_job_worker import SceneJobWorker
from app.services.scene_runner import (
    SceneRunner,
    build_argv,
    is_our_child,
    parse_scenes_csv,
    recover_interrupted_jobs,
    summarize_failure,
    terminate_process_group,
)
from tests.conftest import TestingSessionLocal
from tests.fakes import FakePopen


@pytest.fixture(autouse=True)
def _reset_fake_popen():
    """每个用例开始前清空 FakePopen 实例记录。"""
    FakePopen.reset()
    yield
    FakePopen.reset()


def _make_source(tmp_path, names=("a.mp4", "b.mp4")) -> Path:
    """造一个含假视频的输入目录。"""
    source = tmp_path / "素材"
    source.mkdir(exist_ok=True)
    for name in names:
        (source / name).write_bytes(b"fake-video")
    return source


def _make_job(db_session, tmp_path, mode=SceneJobMode.SPLIT, names=("a.mp4", "b.mp4")) -> SceneJob:
    """通过服务层创建一个真实任务（含枚举与目录预建）。"""
    source = _make_source(tmp_path, names)
    payload = SceneJobCreate(
        input_path=str(source),
        output_dir=str(tmp_path / "输出") if mode == SceneJobMode.SPLIT else None,
        mode=mode,
        template="standard",
    )
    return SceneJobService(db_session).create_job(payload)


def _make_runner(scripts=None, **overrides) -> SceneRunner:
    """构造注入 FakePopen 的执行器。

    Args:
        scripts: 逐次调用 popen 时使用的行为脚本列表；None 表示全部默认成功。
        overrides: 透传给 SceneRunner 的其他参数（如 video_timeout_seconds）。
    """
    script_iter = iter(scripts) if scripts is not None else None

    def factory(*args, **kwargs):
        script = next(script_iter) if script_iter is not None else {}
        return FakePopen(*args, script=script, **kwargs)

    return SceneRunner(
        session_factory=TestingSessionLocal,
        popen_factory=factory,
        tick_seconds=0,
        progress_seconds=0,
        **overrides,
    )


def _refresh(db_session, job: SceneJob) -> SceneJob:
    """重新加载任务（执行器用的是别的会话对象）。"""
    db_session.expire_all()
    return db_session.get(SceneJob, job.id)


class TestBuildArgv:
    """argv 构造：白名单参数映射。"""

    def test_split_full_params(self):
        """split 模式带全部参数。"""
        argv = build_argv(
            "/repo/vct",
            Path("/in/视频.mp4"),
            Path("/out/视频_scenes"),
            {"detector": "content", "threshold": 25.0, "min_len": 1.5, "copy": True},
            split=True,
        )
        assert argv == [
            "/repo/vct", "scene", "/in/视频.mp4",
            "-o", "/out/视频_scenes",
            "--csv", "--split",
            "--detector", "content",
            "--threshold", "25.0",
            "--min-len", "1.5",
            "--copy",
        ]

    def test_preview_omits_split_and_none_threshold(self):
        """预览模式不带 --split；threshold 为 None 时不传该参数（用检测器默认值）。"""
        argv = build_argv(
            "/repo/vct",
            Path("/in/a.mp4"),
            Path("/out"),
            {"detector": "adaptive", "threshold": None, "min_len": 0.6, "copy": False},
            split=False,
        )
        assert "--split" not in argv
        assert "--threshold" not in argv
        assert "--copy" not in argv
        # 未知键不进 argv（params 来自数据库，不信任额外内容）
        argv2 = build_argv(
            "/repo/vct", Path("/in/a.mp4"), Path("/out"),
            {"detector": "adaptive", "evil": "--overwrite"},
            split=False,
        )
        assert "--overwrite" not in argv2


class TestParseScenesCsv:
    """切点 CSV 解析。"""

    def test_parse_real_format(self, tmp_path):
        """按 vct 真实表头解析。"""
        csv_path = tmp_path / "a_scenes.csv"
        csv_path.write_text(
            "序号,开始(秒),结束(秒),时长(秒),开始时间码,结束时间码\n"
            "1,0.000,4.200,4.200,00:00:00.000,00:00:04.200\n"
            "2,4.200,9.000,4.800,00:00:04.200,00:00:09.000\n",
            encoding="utf-8",
        )
        scenes = parse_scenes_csv(csv_path)
        assert scenes == [
            {"number": 1, "start": 0.0, "end": 4.2, "duration": 4.2},
            {"number": 2, "start": 4.2, "end": 9.0, "duration": 4.8},
        ]

    def test_missing_file_returns_empty(self, tmp_path):
        """CSV 不存在时返回空列表而不是崩溃。"""
        assert parse_scenes_csv(tmp_path / "不存在.csv") == []


class TestSummarizeFailure:
    """失败摘要提取：要挑出能说明问题的那一行，而不是一串边框。"""

    def test_prefers_error_line_from_underlying_tool(self):
        """含 Error: 的行是真正的病因，优先取它。"""
        text = (
            "════════════════════\n"
            "  智能镜头分割\n"
            "  输入：损坏素材.mp4\n"
            "════════════════════\n"
            "▶ [1/2] 检测镜头切换\n"
            "Error: Invalid value for -i/--input: Failed to open input video\n"
            "\n"
            "  ✗ 执行失败，退出码 2，耗时 0.3 秒\n"
            "  ✗ 镜头检测失败（退出码 2）\n"
        )
        assert summarize_failure(text).startswith("Error: Invalid value")

    def test_falls_back_to_vct_failure_line(self):
        """没有 Error: 行时，取 vct 自己的 ✗ 失败行。"""
        text = (
            "════════════════════\n"
            "  智能镜头分割\n"
            "════════════════════\n"
            "  ✗ 输出目录不存在\n"
            "  ✗ 切割失败（退出码 3）\n"
        )
        assert summarize_failure(text) == "切割失败（退出码 3）"

    def test_skips_decoration_only_output(self):
        """整篇都是边框时返回空串，由调用方回落到退出码文案。"""
        assert summarize_failure("════════\n────\n\n") == ""

    def test_empty_input(self):
        """空日志不崩。"""
        assert summarize_failure("") == ""


class TestProcessSafety:
    """进程组终止与身份校验。"""

    def test_terminate_nonexistent_group_does_not_raise(self):
        """杀一个不存在的进程组：静默返回，绝不向上抛。"""
        terminate_process_group(999999, grace=0.1)

    def test_is_our_child_rejects_unknown_pid(self):
        """不存在的 PID 一定不是我们起的 vct。"""
        assert is_our_child(999999, "/repo/vct") is False


class TestRunJob:
    """执行主流程。"""

    def test_split_job_success(self, db_session, tmp_path):
        """两条视频全部成功：进度、片段数、切点全部落库。"""
        job = _make_job(db_session, tmp_path)
        runner = _make_runner(scripts=[{"scenes": 3}, {"scenes": 2}])

        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        assert job.status == SceneJobStatus.SUCCESS
        assert job.completed_videos == 2
        assert job.failed_videos == 0
        assert job.clip_count == 5
        assert job.scene_count == 5
        assert job.child_pid is None
        assert job.finished_at is not None

        first, second = job.items
        assert first.status == SceneJobItemStatus.SUCCESS
        assert first.clip_count == 3
        assert first.scene_count == 3
        assert first.exit_code == 0
        assert len(first.scenes) == 3
        assert first.clip_names == [
            "a_clip_001.mp4", "a_clip_002.mp4", "a_clip_003.mp4",
        ]
        assert second.single_shot is False

        # 子进程调用契约：--split + --csv，独立会话，stdio 落文件
        assert len(FakePopen.instances) == 2
        call = FakePopen.instances[0]
        assert "--split" in call.argv and "--csv" in call.argv
        assert call.start_new_session is True
        assert (Path(call.cwd) / "vct.log").exists()

    def test_preview_job_collects_scenes_without_clips(self, db_session, tmp_path):
        """预览模式：只检测不切文件，切点写进 item.scenes。"""
        job = _make_job(db_session, tmp_path, mode=SceneJobMode.PREVIEW, names=("a.mp4",))
        runner = _make_runner(scripts=[{"scenes": 4}])

        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        assert job.status == SceneJobStatus.SUCCESS
        item = job.items[0]
        assert item.scene_count == 4
        assert item.clip_count == 0
        assert len(item.scenes) == 4
        assert "--split" not in FakePopen.instances[0].argv

    def test_single_shot_video_is_success_not_failure(self, db_session, tmp_path):
        """单镜头视频：退出码 0 + 零片段是合法结果，条目标记 single_shot。"""
        job = _make_job(db_session, tmp_path, names=("solo.mp4",))
        runner = _make_runner(scripts=[{"scenes": 1}])

        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        assert job.status == SceneJobStatus.SUCCESS
        item = job.items[0]
        assert item.status == SceneJobItemStatus.SUCCESS
        assert item.single_shot is True
        assert item.clip_count == 0
        assert item.error_message == ""

    def test_partial_clip_failure_keeps_good_clips(self, db_session, tmp_path):
        """部分片段失败（退出码 5 但有产出）：条目标记成功并如实记录失败数。"""
        job = _make_job(db_session, tmp_path, names=("a.mp4",))
        runner = _make_runner(scripts=[{"scenes": 3, "clips": 2, "exit_code": 5}])

        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        assert job.status == SceneJobStatus.SUCCESS
        item = job.items[0]
        assert item.status == SceneJobItemStatus.SUCCESS
        assert item.clip_count == 2
        assert item.failed_clip_count == 1
        assert "部分片段切割失败" in item.error_message

    def test_batch_continues_after_single_failure(self, db_session, tmp_path):
        """第一条失败不中断第二条：任务终态 partial，成功条目的结果保留。"""
        job = _make_job(db_session, tmp_path)
        runner = _make_runner(scripts=[{"exit_code": 1, "clips": 0}, {"scenes": 2}])

        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        assert job.status == SceneJobStatus.PARTIAL
        assert job.failed_videos == 1
        assert job.completed_videos == 2
        first, second = job.items
        assert first.status == SceneJobItemStatus.FAILED
        assert first.error_message != ""
        assert second.status == SceneJobItemStatus.SUCCESS
        assert second.clip_count == 2
        # 两条视频都起了子进程 —— 没有因为第一条失败而中断
        assert len(FakePopen.instances) == 2

    def test_all_failed_marks_job_failed(self, db_session, tmp_path):
        """全部失败 → 任务 failed，错误摘要取自条目。"""
        job = _make_job(db_session, tmp_path, names=("a.mp4",))
        runner = _make_runner(scripts=[{"exit_code": 1, "clips": 0}])

        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        assert job.status == SceneJobStatus.FAILED
        assert job.error_message != ""

    def test_cancel_during_run_skips_remaining(self, db_session, tmp_path):
        """执行中被取消：当前条与后续条目标记 skipped，任务保持 cancelled。"""
        job = _make_job(db_session, tmp_path)
        service = SceneJobService(db_session)

        def cancelling_factory(*args, **kwargs):
            """子进程启动后立刻取消任务，模拟执行中点了取消。"""
            instance = FakePopen(*args, script={"hang": True}, **kwargs)
            service.cancel_job(job.id)
            return instance

        runner = SceneRunner(
            session_factory=TestingSessionLocal,
            popen_factory=cancelling_factory,
            tick_seconds=0,
            progress_seconds=0,
        )
        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        assert job.status == SceneJobStatus.CANCELLED
        assert all(item.status == SceneJobItemStatus.SKIPPED for item in job.items)
        assert job.skipped_videos == 2
        assert job.child_pid is None

    def test_video_timeout_marks_item_failed(self, db_session, tmp_path):
        """单视频硬超时：条目标记 failed 并继续后续视频。"""
        job = _make_job(db_session, tmp_path)
        runner = _make_runner(
            scripts=[{"hang": True}, {"scenes": 2}],
            video_timeout_seconds=0,
            stop_grace_seconds=0.1,
        )

        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        first, second = job.items
        assert first.status == SceneJobItemStatus.FAILED
        assert "超时" in first.error_message
        assert second.status == SceneJobItemStatus.SUCCESS
        assert job.status == SceneJobStatus.PARTIAL

    def test_stop_event_skips_remaining_items(self, db_session, tmp_path):
        """服务停止信号：未执行的条目标记 skipped，任务 failed。"""
        job = _make_job(db_session, tmp_path)
        runner = _make_runner()

        stop = threading.Event()
        stop.set()  # 模拟 lifespan 已请求停止
        assert runner.run_job(job.id, stop_event=stop) is True

        job = _refresh(db_session, job)
        assert job.status == SceneJobStatus.FAILED
        assert all(item.status == SceneJobItemStatus.SKIPPED for item in job.items)
        # 没有起任何子进程
        assert FakePopen.instances == []

    def test_run_job_returns_false_when_not_pending(self, db_session, tmp_path):
        """任务不在 pending 状态（已被取消/抢占）时干净地返回 False。"""
        job = _make_job(db_session, tmp_path)
        SceneJobService(db_session).cancel_job(job.id)

        runner = _make_runner()
        assert runner.run_job(job.id) is False
        assert FakePopen.instances == []


class TestRecover:
    """启动恢复。"""

    def test_recover_marks_interrupted_job_failed(self, db_session, tmp_path):
        """running 任务被回收：任务 failed、running 条目 failed、pending 条目 skipped。"""
        job = _make_job(db_session, tmp_path)
        job.status = SceneJobStatus.RUNNING
        job.child_pid = None  # 不带子进程，避免回收逻辑碰真实进程
        job.items[0].status = SceneJobItemStatus.RUNNING
        db_session.commit()

        recovered = recover_interrupted_jobs(session_factory=TestingSessionLocal)
        assert recovered == 1

        job = _refresh(db_session, job)
        assert job.status == SceneJobStatus.FAILED
        assert "服务重启" in job.error_message
        assert job.items[0].status == SceneJobItemStatus.FAILED
        assert job.items[1].status == SceneJobItemStatus.SKIPPED

    def test_recover_leaves_pending_jobs_alone(self, db_session, tmp_path):
        """pending 任务不动 —— DB 队列会自动接手。"""
        job = _make_job(db_session, tmp_path)

        recovered = recover_interrupted_jobs(session_factory=TestingSessionLocal)
        assert recovered == 0

        job = _refresh(db_session, job)
        assert job.status == SceneJobStatus.PENDING


class TestWorker:
    """工作线程（全仓库唯一真起线程的用例，完全不碰 DB）。"""

    def test_worker_drains_queue_then_idles(self):
        """按顺序执行认领到任务，队列空后空转，stop 能立即停下。"""
        executed = []
        queue = iter([11, 22])

        class FakeRunner:
            def run_job(self, job_id, stop_event=None):
                executed.append(job_id)
                return True

        worker = SceneJobWorker(
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
        assert worker._thread is None

    def test_worker_survives_claim_exception(self):
        """认领抛异常时线程不死亡，继续下一轮。"""
        calls = {"count": 0}

        def flaky_claim():
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("模拟数据库抖动")
            return None

        worker = SceneJobWorker(
            claim_next=flaky_claim,
            runner_factory=lambda: SceneRunner(
                session_factory=TestingSessionLocal, tick_seconds=0
            ),
            poll_seconds=0.01,
        )
        worker.start()
        try:
            threading.Event().wait(0.1)
            # 第一次抛异常后仍继续轮询
            assert calls["count"] >= 2
        finally:
            worker.stop(grace=2)
