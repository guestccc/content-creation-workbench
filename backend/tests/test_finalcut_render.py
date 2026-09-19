"""一键成品·合成执行器（finalcut_render_runner）的测试。

用 FakeFfmpeg 注入 popen_factory，在主线程同步跑完「逐条烧字」全流程，
不需要真视频、不起真子进程。覆盖：
- 成功全流程（产物就位、临时目录清掉、argv 形状抽查）；
- 单条失败 → partial、全部失败 → failed（失败时临时目录与 ffmpeg 日志保留）；
- 取消 → 当前条 skipped、剩余条 skipped、临时目录清掉；
- 环境/素材前置失败（ffmpeg 不可用 / 无 drawtext / 字体缺失 / 视频丢失）→
  一次 ffmpeg 都不起，报的是环境类原因；
- 音频编码随 video_spec 快照走（aac 流复制 / 无音轨 -an）；
- 进度刷新与 recover_interrupted_render_jobs。
"""

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.finalcut_job import (
    FinalcutItemStatus,
    FinalcutRenderItem,
    FinalcutRenderJob,
    FinalcutRenderJobStatus,
)
from app.services import finalcut_render_runner as render_runner
from app.services.finalcut_render_runner import (
    FinalcutRenderRunner,
    claim_next_pending_id,
    recover_interrupted_render_jobs,
)
from tests.conftest import TestingSessionLocal
from tests.fakes import FakeFfmpeg

#: 固定的视频规格快照（创建任务时由 ffprobe 探测落库，runner 只认这个）
_SPEC = {
    "width": 720, "height": 1280, "fps_num": 30, "fps_den": 1,
    "has_audio": True, "audio_codec": "aac",
}

_CAPS_OK = {
    "ok": True, "path": "/fake/ffmpeg", "version": "7.1",
    "has_drawtext": True, "supports_boxborderw": True,
    "detail": "ffmpeg 7.1，drawtext 可用", "fix_hint": "",
}


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeFfmpeg.reset()
    yield
    FakeFfmpeg.reset()


@pytest.fixture()
def materials(tmp_path, monkeypatch):
    """素材根指向临时目录（runner 的 .tmp 临时目录挂在这下面）。"""
    root = tmp_path / "materials"
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(root))
    return root


@pytest.fixture(autouse=True)
def _stub_tools(monkeypatch):
    """打桩 ffmpeg 三件套：找到它、它自证是真的、drawtext 能力实测通过。

    都打在被测模块自己的命名空间上（runner 用的是 import 进来的名字）。
    """
    monkeypatch.setattr(render_runner, "find_tool", lambda tool: "/fake/ffmpeg")
    monkeypatch.setattr(render_runner, "verify_tool", lambda tool, path: "7.1")
    monkeypatch.setattr(render_runner, "probe_drawtext", lambda path=None: dict(_CAPS_OK))


def _make_job(db_session, tmp_path, *, copies=("文案一",), video_exists=True,
              font_exists=True, spec=None, font_size=0, status=None) -> FinalcutRenderJob:
    """直接落库一个 pending 合成任务（绕开服务层，runner 测试不碰创建校验）。"""
    video = tmp_path / "成片.mp4"
    if video_exists:
        video.write_bytes(b"fake-src-video")
    font = tmp_path / "font.ttf"
    if font_exists:
        font.write_bytes(b"fake-font-bytes")

    job = FinalcutRenderJob(
        status=status or FinalcutRenderJobStatus.PENDING,
        video_path=str(video),
        video_duration=15.0,
        video_spec=dict(spec or _SPEC),
        output_dir=str(tmp_path / "output" / "finalcut-test"),
        font_file=str(font) if font_exists else "",
        total_items=len(copies),
    )
    db_session.add(job)
    db_session.flush()
    for index, text in enumerate(copies, start=1):
        db_session.add(
            FinalcutRenderItem(
                job_id=job.id, index=index, copy_text=text,
                angle="痛点开场", style="white_box",
                box={"x": 0.1, "y": 0.7, "w": 0.8, "h": 0.2},
                font_size=font_size,
            )
        )
    db_session.commit()
    return job


def _make_runner(scripts=None, **overrides) -> FinalcutRenderRunner:
    script_iter = iter(scripts) if scripts is not None else None

    def factory(*args, **kwargs):
        script = next(script_iter) if script_iter is not None else {}
        return FakeFfmpeg(*args, script=script, **kwargs)

    options = {"tick_seconds": 0, "progress_seconds": 0}
    options.update(overrides)
    return FinalcutRenderRunner(
        session_factory=TestingSessionLocal,
        popen_factory=factory,
        **options,
    )


def _reload(db_session, job_id: int) -> FinalcutRenderJob:
    db_session.expire_all()
    return db_session.get(FinalcutRenderJob, job_id)


def _tmp_dir(materials, job_id: int):
    return materials / "finalcut" / ".tmp" / f"finalcut_{job_id}"


# --------------------------------------------------------------------------
# 成功与部分失败
# --------------------------------------------------------------------------


class TestRunJob:
    def test_success_two_items(self, db_session, materials, tmp_path):
        job = _make_job(db_session, tmp_path, copies=("第一条文案", "第二条文案"))
        runner = _make_runner(scripts=[{}, {}])
        assert runner.run_job(job.id) is True

        job = _reload(db_session, job.id)
        assert job.status == FinalcutRenderJobStatus.SUCCESS
        assert job.completed_items == 2 and job.failed_items == 0
        assert job.error_message == ""
        assert job.finished_at is not None
        # 任务结束后「当前正在做什么」字段清零
        assert job.current_index == 0 and job.progress_percent == 0.0
        assert job.child_pid is None

        out_dir = tmp_path / "output" / "finalcut-test"
        for index, item in enumerate(job.items, start=1):
            assert item.status == FinalcutItemStatus.SUCCESS
            assert item.exit_code == 0
            assert item.output_name == f"{index:02d}.mp4"
            assert item.duration_seconds == 15.0
            assert item.size_bytes > 0
            assert item.resolved_font_size > 0
            # 成片真实就位，半成品改名后不应留下
            assert (out_dir / f"{index:02d}.mp4").read_bytes() == b"fake-mp4"
            assert not (out_dir / f"{index:02d}.partial.mp4").exists()
        # 全部成功 → 临时目录整个清掉
        assert not _tmp_dir(materials, job.id).exists()

    def test_success_argv_shape(self, db_session, materials, tmp_path):
        """抽查 argv：滤镜是 cwd 相对名、输出是 .partial、音频按 spec 走 copy。"""
        job = _make_job(db_session, tmp_path, copies=("文案",))
        _make_runner().run_job(job.id)

        assert len(FakeFfmpeg.instances) == 1
        argv = FakeFfmpeg.instances[0].argv
        filter_str = argv[argv.index("-vf") + 1]
        assert "fontfile=font.ttf" in filter_str and "textfile=copy.txt" in filter_str
        assert "\\" not in filter_str
        assert argv[argv.index("-c:a") + 1] == "copy"  # spec 里 audio_codec=aac
        assert argv[-1].endswith("01.partial.mp4")

    def test_no_audio_codec_uses_an(self, db_session, materials, tmp_path):
        spec = {**_SPEC, "has_audio": False, "audio_codec": ""}
        job = _make_job(db_session, tmp_path, copies=("文案",), spec=spec)
        _make_runner().run_job(job.id)

        argv = FakeFfmpeg.instances[0].argv
        assert "-an" in argv and "-c:a" not in argv

    def test_manual_font_size_respected(self, db_session, materials, tmp_path):
        job = _make_job(db_session, tmp_path, copies=("文案",), font_size=48)
        _make_runner().run_job(job.id)

        item = _reload(db_session, job.id).items[0]
        assert item.resolved_font_size == 48

    def test_one_failure_yields_partial(self, db_session, materials, tmp_path):
        """第 2 条失败：任务 partial，成功的成片保留，临时目录留下（里面有日志）。"""
        job = _make_job(db_session, tmp_path, copies=("好的", "坏的"))
        runner = _make_runner(scripts=[{}, {"exit_code": 1, "produce": False}])
        runner.run_job(job.id)

        job = _reload(db_session, job.id)
        assert job.status == FinalcutRenderJobStatus.PARTIAL
        assert job.failed_items == 1
        assert job.items[0].status == FinalcutItemStatus.SUCCESS
        assert job.items[1].status == FinalcutItemStatus.FAILED
        assert "第2条" in job.error_message
        # 半成品必须清掉，不能以任何名字留下
        out_dir = tmp_path / "output" / "finalcut-test"
        assert (out_dir / "01.mp4").is_file()
        assert not (out_dir / "02.mp4").exists()
        assert not (out_dir / "02.partial.mp4").exists()
        # 有失败 → 临时目录保留，ffmpeg 日志在里面
        assert (_tmp_dir(materials, job.id) / "ffmpeg_item_02.log").is_file()

    def test_all_fail_yields_failed(self, db_session, materials, tmp_path):
        job = _make_job(db_session, tmp_path, copies=("甲", "乙"))
        runner = _make_runner(scripts=[{"exit_code": 1}, {"exit_code": 1}])
        runner.run_job(job.id)

        job = _reload(db_session, job.id)
        assert job.status == FinalcutRenderJobStatus.FAILED
        assert job.failed_items == 2
        assert "第1条" in job.error_message and "第2条" in job.error_message
        assert _tmp_dir(materials, job.id).is_dir()

    def test_exit_zero_but_no_output_is_failure(self, db_session, materials, tmp_path):
        """退出码 0 但产物缺失（假成功）同样算失败。"""
        job = _make_job(db_session, tmp_path, copies=("文案",))
        _make_runner(scripts=[{"produce": False}]).run_job(job.id)

        job = _reload(db_session, job.id)
        assert job.status == FinalcutRenderJobStatus.FAILED
        assert "未产出成片" in job.items[0].error_message

    def test_run_job_only_claims_pending(self, db_session, materials, tmp_path):
        job = _make_job(db_session, tmp_path, status=FinalcutRenderJobStatus.RUNNING)
        assert _make_runner().run_job(job.id) is False
        assert FakeFfmpeg.instances == []

    def test_claim_picks_earliest_pending(self, db_session, materials, tmp_path):
        first = _make_job(db_session, tmp_path)
        _make_job(db_session, tmp_path)
        assert claim_next_pending_id(session_factory=TestingSessionLocal) == first.id


# --------------------------------------------------------------------------
# 取消与进度
# --------------------------------------------------------------------------


class TestCancelAndProgress:
    def test_cancel_during_first_item(self, db_session, materials, tmp_path):
        """第 1 条烧到一半取消：当前条 skipped，剩余条 skipped，ffmpeg 只起一次。"""
        job = _make_job(db_session, tmp_path, copies=("甲", "乙", "丙"))

        def cancel_on_poll(proc, poll_count):
            if poll_count == 1:
                with TestingSessionLocal() as db:
                    db.query(FinalcutRenderJob).filter_by(id=job.id).update(
                        {"status": FinalcutRenderJobStatus.CANCELLED}
                    )
                    db.commit()

        scripts = [{"hang": True, "on_poll": cancel_on_poll}]
        _make_runner(scripts=scripts).run_job(job.id)

        job = _reload(db_session, job.id)
        assert job.status == FinalcutRenderJobStatus.CANCELLED
        assert [item.status for item in job.items] == [
            FinalcutItemStatus.SKIPPED,
            FinalcutItemStatus.SKIPPED,
            FinalcutItemStatus.SKIPPED,
        ]
        assert job.skipped_items == 3
        # 取消发生在第 1 条：后面的 ffmpeg 一次都不该起
        assert len(FakeFfmpeg.instances) == 1
        # 取消 → 半成品清掉、临时目录整个删掉
        out_dir = tmp_path / "output" / "finalcut-test"
        assert not (out_dir / "01.partial.mp4").exists()
        assert not _tmp_dir(materials, job.id).exists()

    def test_progress_percent_from_ffmpeg_progress_file(
        self, db_session, materials, tmp_path
    ):
        """进度 = ffmpeg out_time_us / 视频时长；跑到一半时轮询能看到中间值。"""
        job = _make_job(db_session, tmp_path, copies=("文案",))
        tmp_dir = _tmp_dir(materials, job.id)
        observed = []

        def probe(proc, poll_count):
            if poll_count == 1:
                # 模拟 ffmpeg 写到一半：7.5s / 15s = 50%
                tmp_dir.mkdir(parents=True, exist_ok=True)
                (tmp_dir / "progress.txt").write_text(
                    "out_time_us=7500000\nprogress=continue\n", encoding="utf-8"
                )
            if poll_count == 2:
                with TestingSessionLocal() as db:
                    value = db.execute(
                        select(FinalcutRenderJob.progress_percent)
                        .where(FinalcutRenderJob.id == job.id)
                    ).scalar_one()
                observed.append(value)

        scripts = [{"polls_before_exit": 3, "on_poll": probe}]
        _make_runner(scripts=scripts).run_job(job.id)

        assert observed == [50.0]
        # 任务结束后进度字段清零（终态任务的进度不该停在半中间）
        assert _reload(db_session, job.id).progress_percent == 0.0


# --------------------------------------------------------------------------
# 环境/素材前置失败（一次 ffmpeg 都不起）
# --------------------------------------------------------------------------


class TestPreflightFailures:
    def test_bogus_ffmpeg(self, db_session, materials, tmp_path, monkeypatch):
        """名字叫 ffmpeg 的东西自证失败 → 环境类报错，不起任何子进程。"""
        monkeypatch.setattr(render_runner, "verify_tool", lambda tool, path: "")
        job = _make_job(db_session, tmp_path, copies=("甲", "乙"))
        _make_runner().run_job(job.id)

        job = _reload(db_session, job.id)
        assert job.status == FinalcutRenderJobStatus.FAILED
        assert "ffmpeg 不可用" in job.error_message
        assert all(item.status == FinalcutItemStatus.FAILED for item in job.items)
        assert FakeFfmpeg.instances == []

    def test_drawtext_missing(self, db_session, materials, tmp_path, monkeypatch):
        monkeypatch.setattr(
            render_runner, "probe_drawtext",
            lambda path=None: {
                **_CAPS_OK, "ok": False,
                "detail": "这个 ffmpeg 构建里没有 drawtext 滤镜",
                "fix_hint": "换 gyan.dev 的 full build",
            },
        )
        job = _make_job(db_session, tmp_path)
        _make_runner().run_job(job.id)

        job = _reload(db_session, job.id)
        assert job.status == FinalcutRenderJobStatus.FAILED
        assert "drawtext" in job.error_message
        assert "full build" in job.error_message  # 修复指引一并给出
        assert FakeFfmpeg.instances == []

    def test_font_missing(self, db_session, materials, tmp_path):
        job = _make_job(db_session, tmp_path, font_exists=False)
        _make_runner().run_job(job.id)

        job = _reload(db_session, job.id)
        assert job.status == FinalcutRenderJobStatus.FAILED
        assert "中文字体" in job.error_message
        assert FakeFfmpeg.instances == []

    def test_video_missing(self, db_session, materials, tmp_path):
        job = _make_job(db_session, tmp_path, video_exists=False)
        _make_runner().run_job(job.id)

        job = _reload(db_session, job.id)
        assert job.status == FinalcutRenderJobStatus.FAILED
        assert "成片视频不存在" in job.error_message
        assert FakeFfmpeg.instances == []

    def test_missing_video_spec(self, db_session, materials, tmp_path):
        job = _make_job(db_session, tmp_path, spec={"width": 0, "height": 0})
        _make_runner().run_job(job.id)

        job = _reload(db_session, job.id)
        assert job.status == FinalcutRenderJobStatus.FAILED
        assert "视频规格" in job.error_message
        assert FakeFfmpeg.instances == []


# --------------------------------------------------------------------------
# 孤儿回收
# --------------------------------------------------------------------------


class TestRecover:
    def test_recovers_running_job(self, db_session, materials, tmp_path):
        job = _make_job(db_session, tmp_path, copies=("甲", "乙"),
                        status=FinalcutRenderJobStatus.RUNNING)
        items = db_session.query(FinalcutRenderItem).filter_by(job_id=job.id).all()
        items[0].status = FinalcutItemStatus.RUNNING
        job.child_pid = 4_000_000  # 必然不存在的 PID：核对失败，不会误杀
        db_session.commit()

        recovered = recover_interrupted_render_jobs(session_factory=TestingSessionLocal)
        assert recovered == 1

        job = _reload(db_session, job.id)
        assert job.status == FinalcutRenderJobStatus.FAILED
        assert job.error_message == "服务重启，任务中断"
        assert job.child_pid is None
        assert job.items[0].status == FinalcutItemStatus.FAILED
        assert job.items[1].status == FinalcutItemStatus.SKIPPED

    def test_pending_jobs_untouched(self, db_session, materials, tmp_path):
        job = _make_job(db_session, tmp_path)
        recovered = recover_interrupted_render_jobs(session_factory=TestingSessionLocal)
        assert recovered == 0
        assert _reload(db_session, job.id).status == FinalcutRenderJobStatus.PENDING
