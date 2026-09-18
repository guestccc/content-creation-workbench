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
from app.services import scene_runner as scene_runner_module
from app.services.scene_job_service import SceneJobService
from app.services.scene_job_worker import SceneJobWorker
from app.services.scene_runner import (
    SceneRunner,
    build_argv,
    is_our_child,
    parse_progress,
    parse_scenes_csv,
    read_log_tail,
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
        overrides: 透传给 SceneRunner 的其他参数（如 video_timeout_seconds、
            progress_seconds），会盖掉上面两项默认值。
    """
    script_iter = iter(scripts) if scripts is not None else None

    def factory(*args, **kwargs):
        script = next(script_iter) if script_iter is not None else {}
        return FakePopen(*args, script=script, **kwargs)

    # 默认 tick / progress 都设成 0：测试里子进程是假的，没什么可等。
    # 要验证节流本身的用例用 overrides 覆盖掉这两项。
    options = {"tick_seconds": 0, "progress_seconds": 0}
    options.update(overrides)
    return SceneRunner(
        session_factory=TestingSessionLocal,
        popen_factory=factory,
        **options,
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


# 一段真实形态的 vct.log：参数回显 → 检测 → 逐段切割上报。
# 中间特意夹了一行「正文里提到标记」的文本，用来验证解析只认行首的标记。
REAL_LOG = """\
════════════════════════════════════════
  智能镜头分割
  输入：素材.mp4
════════════════════════════════════════
  · 说明里出现 #vct-progress 不算标记，因为没顶在行首
▶ [1/2] 检测镜头切换
#vct-progress detect 0 1
▍ 镜头切点（检测器 自适应，最短镜头 0.6s）
▶ [2/2] 切割 3 个片段
#vct-progress split 0 3
  ✓ [1/3] 素材_clip_001.mp4  2.00s
#vct-progress split 1 3
  ✓ [2/3] 素材_clip_002.mp4  2.50s
#vct-progress split 2 3
  ✓ [3/3] 素材_clip_003.mp4  3.00s
#vct-progress split 3 3
  ✓ 切割完成：3 个片段
"""


class TestParseProgress:
    """vct 的机器可读进度标记。

    这是 vct（vctl/ui.py）与工作台之间的契约：前缀、字段顺序、行首位置都
    钉死。解析一旦悄悄失效，用户看到的就是一条永远不动的进度条。
    """

    def test_reads_phase_done_total(self):
        assert parse_progress("#vct-progress split 3 38") == {
            "phase": "split", "done": 3, "total": 38,
        }

    def test_takes_the_last_marker(self):
        """标记单调前进，日志尾部那条才是当前进度。"""
        assert parse_progress(REAL_LOG) == {"phase": "split", "done": 3, "total": 3}

    def test_detect_phase_is_reported(self):
        """检测阶段也要报 —— 没有它，前端在切割开始前完全看不见动静。"""
        assert parse_progress("#vct-progress detect 0 1") == {
            "phase": "detect", "done": 0, "total": 1,
        }

    def test_no_marker_returns_none(self):
        """老版本 vct 的人类可读日志：解析不出标记，调用方据此降级。"""
        text = (
            "▶ [1/2] 检测镜头切换\n"
            "▶ [2/2] 切割 38 个片段\n"
            "  ✓ [1/38] 素材_clip_001.mp4  2.00s\n"
        )
        assert parse_progress(text) is None

    def test_marker_must_start_the_line(self):
        """行首之外出现的前缀不算标记，否则日志里提到它就误判。"""
        assert parse_progress("  #vct-progress split 1 3") is None
        assert parse_progress("见 #vct-progress split 1 3") is None

    def test_malformed_marker_ignored(self):
        """字段缺失或不是数字的行整行作废，不能猜。"""
        for bad in (
            "#vct-progress split",
            "#vct-progress split 1",
            "#vct-progress split x 3",
            "#vct-progress split 1 3 4",
        ):
            assert parse_progress(bad) is None, bad

    def test_bad_line_does_not_poison_a_good_one(self):
        text = "#vct-progress split 坏了\n#vct-progress split 2 5\n"
        assert parse_progress(text) == {"phase": "split", "done": 2, "total": 5}

    def test_truncated_tail_still_yields_latest_marker(self, tmp_path):
        """日志尾部是按字节截的，首行可能是半截 —— 那不该影响后面的标记。"""
        log_path = tmp_path / "vct.log"
        log_path.write_text(REAL_LOG, encoding="utf-8")
        # 只读最后 60 字节：足以切断前面的行，但完整包含最后一条标记
        assert parse_progress(read_log_tail(log_path, limit=60)) == {
            "phase": "split", "done": 3, "total": 3,
        }


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


class TestProgressReporting:
    """执行中的实时进度：勾选多条视频时，每一条都要有自己的进展。

    取材于真实使用场景 —— 一次勾十条视频，界面上十行，用户要能看出「现在
    在切第几条、这条切到第几个片段了」。前端每两秒轮询一次任务详情接口，
    所以这里用 FakePopen 的 on_poll 回调打开一个新会话读当时的进度字段，
    等价于模拟那个轮询接口在真实时间轴上会看到什么。
    """

    @staticmethod
    def _poller(job_id, snapshots):
        """造一个「轮询接口」：每次子进程被 poll 时读一次当前进度。"""
        def probe(_proc, _count):
            with TestingSessionLocal() as db:
                job = db.get(SceneJob, job_id)
                snapshots.append(
                    (job.current_index, job.current_phase,
                     job.current_clips, job.current_total_clips)
                )
        return probe

    def test_each_video_reports_its_own_progress(self, db_session, tmp_path):
        """第一条视频走到 2/3 时进度如实；换到第二条时清零重来。"""
        job = _make_job(db_session, tmp_path)
        snapshots = []
        probe = self._poller(job.id, snapshots)

        # 第一条：检测 → 分母出来 → 逐段前进（共 4 次空转 poll，每 poll 吐一行标记）
        first = {
            "scenes": 3,
            "polls_before_exit": 4,
            "log_lines": [
                "#vct-progress detect 0 1",
                "#vct-progress split 0 3",
                "#vct-progress split 1 3",
                "#vct-progress split 2 3",
            ],
            "on_poll": probe,
        }
        second = {"scenes": 2, "on_poll": probe}
        runner = _make_runner(scripts=[first, second])

        assert runner.run_job(job.id) is True

        # 序：第 1 条的第 1 次快照（此时还没跑到第一次刷进度）、检测中、
        # 分母出来、1/3、2/3；然后换第 2 条，第一眼必须是干净的。
        assert snapshots == [
            (1, "", 0, 0),        # 刚认领，进度还没开始刷
            (1, "detect", 0, 0),  # 检测中 —— 没有分母，不编
            (1, "split", 0, 3),   # 检测完，分母终于出来了
            (1, "split", 1, 3),
            (1, "split", 2, 3),
            (2, "", 0, 0),        # 换第二条：不带上一条的残留
        ]

    def test_progress_is_cleared_when_job_finishes(self, db_session, tmp_path):
        """任务跑完不留残影：没有「当前视频」了，实时进度字段一律归零。"""
        job = _make_job(db_session, tmp_path, names=("a.mp4",))
        runner = _make_runner(scripts=[{
            "scenes": 3,
            "polls_before_exit": 2,
            "log_lines": ["#vct-progress detect 0 1", "#vct-progress split 2 3"],
        }])

        assert runner.run_job(job.id) is True

        job = _refresh(db_session, job)
        assert job.status == SceneJobStatus.SUCCESS
        assert job.current_video == ""
        assert job.current_phase == ""
        assert job.current_clips == 0
        assert job.current_total_clips == 0
        # 成色数据不受影响，仍在条目上
        assert job.items[0].clip_count == 3

    def test_cleared_after_cancel_too(self, db_session, tmp_path):
        """取消同样要归零 —— 否则界面上留着一条停在「切割中」的进度。"""
        job = _make_job(db_session, tmp_path)
        service = SceneJobService(db_session)

        def cancelling_factory(*args, **kwargs):
            instance = FakePopen(
                *args,
                script={
                    "hang": True,
                    "log_lines": ["#vct-progress split 1 9"],
                },
                **kwargs,
            )
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
        assert job.current_phase == ""
        assert job.current_total_clips == 0
        assert job.current_clips == 0

    def test_single_shot_never_publishes_a_split_denominator(self, db_session, tmp_path):
        """单镜头视频只报检测：分母是「检测这一步」的 1，不能当成片段数。"""
        job = _make_job(db_session, tmp_path, names=("solo.mp4",))
        snapshots = []
        runner = _make_runner(scripts=[{
            "scenes": 1,
            "polls_before_exit": 1,
            "log_lines": ["#vct-progress detect 0 1"],
            "on_poll": self._poller(job.id, snapshots),
        }])

        assert runner.run_job(job.id) is True

        assert (1, "detect", 0, 0) in snapshots
        # 全程没有出现过任何非零分母
        assert all(total == 0 for _, phase, _, total in snapshots if phase != "split")

    def test_failed_clip_still_moves_the_counter_forward(self, db_session, tmp_path):
        """分子取 vct 上报的序号而不是文件数：有片段切失败时进度也要走完。

        否则 38 段里坏 1 段，进度条会永远停在 37/38 —— 看上去像卡死了。
        这一条的成色由 item.clip_count / failed_clip_count 另行如实呈现。
        """
        job = _make_job(db_session, tmp_path, names=("a.mp4",))
        out_dir = Path(job.items[0].output_dir)
        # 磁盘上只有 2 个片段（第 3 段切失败了），日志却报到了 3/3
        for index in (1, 2):
            (out_dir / f"a_clip_{index:03d}.mp4").write_bytes(b"fake")
        (out_dir / "vct.log").write_text(
            "#vct-progress split 0 3\n#vct-progress split 3 3\n", encoding="utf-8"
        )

        runner = _make_runner()
        with TestingSessionLocal() as db:
            live = db.get(SceneJob, job.id)
            live.current_index = 1  # 正常执行时由 _run_item 设置，这里直接补上
            runner._refresh_job_progress(db, live)

        live = _refresh(db_session, job)
        assert live.current_phase == "split"
        assert live.current_total_clips == 3
        assert live.current_clips == 3          # 序号走完了
        assert live.current_clip_names == [     # 产物视角仍然是 2 个
            "a_clip_001.mp4", "a_clip_002.mp4",
        ]

    def test_old_vct_without_markers_falls_back_to_file_count(self, db_session, tmp_path):
        """老版本 vct 不发标记：至少靠产物数让用户看到「这条在往前走」。"""
        job = _make_job(db_session, tmp_path, names=("a.mp4",))
        out_dir = Path(job.items[0].output_dir)
        for index in (1, 2, 3):
            (out_dir / f"a_clip_{index:03d}.mp4").write_bytes(b"fake")
        (out_dir / "vct.log").write_text(
            "▶ [2/2] 切割 3 个片段\n  ✓ [1/3] a_clip_001.mp4  2.00s\n",
            encoding="utf-8",
        )

        runner = _make_runner()
        with TestingSessionLocal() as db:
            live = db.get(SceneJob, job.id)
            live.current_index = 1
            runner._refresh_job_progress(db, live)

        live = _refresh(db_session, job)
        assert live.current_clips == 3      # 退化成数文件
        assert live.current_phase == ""     # 阶段不知道就是不知道，不猜
        assert live.current_total_clips == 0

    def test_transient_log_truncation_keeps_the_last_numbers(self, db_session, tmp_path):
        """日志尾部偶尔截断在标记行中间时，进度原地保持，不跳回文件数。

        尾部是按字节截的（read_log_tail），极端情况下最后一条标记会被切断 ——
        那不该让进度从 5/9 掉回 3/9 再跳回去，用户看到的是一根抽搐的进度条。
        """
        job = _make_job(db_session, tmp_path, names=("a.mp4",))
        out_dir = Path(job.items[0].output_dir)
        for index in (1, 2, 3):
            (out_dir / f"a_clip_{index:03d}.mp4").write_bytes(b"fake")
        # 日志尾部没有任何完整标记（模拟被截断）
        (out_dir / "vct.log").write_text("  ✓ [3/9] a_clip_003.mp4  2.00s\n", encoding="utf-8")

        runner = _make_runner()
        with TestingSessionLocal() as db:
            live = db.get(SceneJob, job.id)
            live.current_index = 1
            live.current_phase = "split"          # 上一轮读到过标记
            live.current_total_clips = 9
            live.current_clips = 5
            runner._refresh_job_progress(db, live)

        live = _refresh(db_session, job)
        assert live.current_clips == 5            # 保持，不掉回 3
        assert live.current_total_clips == 9
        assert live.current_phase == "split"
        assert len(live.current_clip_names) == 3   # 产物视角照常更新

    def test_progress_is_reset_before_the_next_video_starts(self, db_session, tmp_path):
        """换视频时先清零再起子进程 —— 否则新视频顶着上一条的「38/38」开场。"""
        job = _make_job(db_session, tmp_path)
        # 第一条跑到 3/3 结束，中间留一次刷新
        runner = _make_runner(scripts=[
            {"scenes": 3, "polls_before_exit": 1,
             "log_lines": ["#vct-progress split 3 3"]},
            {"scenes": 2},
        ])
        seen = []
        runner.popen_factory = _spy_factory(runner.popen_factory, seen, job.id)

        assert runner.run_job(job.id) is True

        # 每次起子进程的那一刻，进度字段都是干净的 —— 第二条尤其重要，
        # 它要是继承上一条的 3/3，用户在新视频开头会看到一条满格的假进度。
        assert seen == [(1, "", 0, 0), (2, "", 0, 0)]

    def test_writes_immediately_then_throttles(self, db_session, tmp_path, monkeypatch):
        """第一次刷新在子进程刚起来时就落库，之后按节流周期刷。

        生产配置是 SCENE_JOB_PROGRESS_SECONDS=3：每 3 秒写一次库，别把 SQLite
        敲出火花。但**第一次不能等** —— 等了的话，一条几秒钟就切完的小视频
        从头到尾一个进度都刷不出来（真实端到端跑过：8 秒的素材整条跑完只用了
        .4 秒，一次写库都没赶上）。所以计数从 0 开始，第一轮必然命中。

        这里把时钟换成假的，免得测试真的睡 3 秒 —— 时间成了参数，断言才有意义。
        """
        job = _make_job(db_session, tmp_path, names=("a.mp4",))
        clock = _FakeClock()
        monkeypatch.setattr(scene_runner_module, "time", clock)

        snapshots = []
        probe = self._poller(job.id, snapshots)

        def tick(_proc, _count):
            # 每次 poll 推进 1 秒，等价的真实节奏下每 poll 间隔约 1 秒
            clock.advance(1.0)
            probe(_proc, _count)

        runner = _make_runner(scripts=[{
            "scenes": 3,
            "polls_before_exit": 4,
            "log_lines": [
                "#vct-progress detect 0 1",
                "#vct-progress split 0 3",
                "#vct-progress split 1 3",
                "#vct-progress split 2 3",
                "#vct-progress split 3 3",
            ],
            "on_poll": tick,
        }], progress_seconds=3.0)

        assert runner.run_job(job.id) is True

        # 第 1 眼还没有东西可报；第 2 眼就已经是「检测中」了 —— 没等满 3 秒。
        # 之后连看 3 眼都是「检测中」：节流生效，中间的标记不落地。
        # 第 5 眼才跳到日志里最后一条标记（第 4 轮 poll 时刷的那次）。
        assert snapshots == [
            (1, "", 0, 0),        # 刚起进程，一次都还没刷
            (1, "detect", 0, 0),  # 第一轮回合就写了，用户不用干等 3 秒
            (1, "detect", 0, 0),
            (1, "detect", 0, 0),
            (1, "split", 2, 3),   # 满 3 秒后的那次刷新读到了最新标记
        ]


class _FakeClock:
    """假时钟：把「等了多久」变成测试可以拨的参数。

    只实现 scene_runner 用到的那两个方法；sleep 空转，因为时间由
    advance() 显式推进，真睡下去只会拖慢测试。
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def monotonic(self) -> float:
        return self._now

    def sleep(self, _seconds: float) -> None:
        pass


def _spy_factory(popen_factory, seen, job_id):
    """包一层 popen 工厂：每次起子进程时记录当时的进度字段。

    真实 vct 起进程前会先在 out_dir 里截断 vct.log，所以「起进程那一刻的
    进度」等价于用户在新视频开头看到的那一眼。
    """
    def factory(*args, **kwargs):
        with TestingSessionLocal() as db:
            live = db.get(SceneJob, job_id)
            seen.append(
                (live.current_index, live.current_phase,
                 live.current_clips, live.current_total_clips)
            )
        return popen_factory(*args, **kwargs)
    return factory


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
