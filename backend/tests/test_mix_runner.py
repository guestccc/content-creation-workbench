"""混剪执行器（mix_runner）的测试。

三组：
1. 编排纯函数 plan_outputs —— 顺序规则、去重、排列数上限、种子可复现；
2. ffmpeg 命令构造 —— 滤镜链、concat 清单转义、argv 关键参数；
3. run_job —— 用 FakeFfmpeg 注入，在主线程同步跑完两遍编码全流程，
   覆盖成功 / 单片段归一化失败拖垮成片 / 拼接失败得 partial / 取消得 skipped。

所有 ffprobe 探测与 find_tool 都打桩，测试不需要真视频、不跑真子进程。
"""

import time
from pathlib import Path
from typing import List, Optional

import pytest

from app.core.config import settings
from app.models.mix_job import (
    MixJob,
    MixJobItem,
    MixJobStatus,
    MixOutputStatus,
    MixPhase,
)
from app.core.materials import CLIPS, subdir
from app.services import mix_runner
from app.services.mix_runner import (
    MixRunner,
    build_concat_argv,
    build_normalize_argv,
    build_normalize_filter,
    middle_permutation_limit,
    plan_outputs,
    probe_environment,
    read_ffmpeg_progress_us,
    recover_interrupted_jobs,
    resolve_clip_path,
    write_concat_list,
)
from tests.conftest import TestingSessionLocal


# --------------------------------------------------------------------------
# 测试替身：模拟 ffmpeg 子进程
# --------------------------------------------------------------------------


class FakeFfmpeg:
    """模拟 ffmpeg 子进程：退出时在 argv 最后一个参数的位置「产出」文件。"""

    instances: List["FakeFfmpeg"] = []

    @classmethod
    def reset(cls) -> None:
        cls.instances = []

    def __init__(self, argv, stdout=None, stderr=None, stdin=None,
                 start_new_session=False, creationflags=0, cwd=None, env=None, script=None):
        self.argv = list(argv)
        self.script = dict(script or {})
        self.stdout = stdout
        self.pid = 50000 + len(FakeFfmpeg.instances)
        self.poll_count = 0
        self._produced = False
        FakeFfmpeg.instances.append(self)

    def poll(self) -> Optional[int]:
        self.poll_count += 1
        on_poll = self.script.get("on_poll")
        if on_poll is not None:
            on_poll(self, self.poll_count)
        if self.script.get("hang"):
            return None
        if self.poll_count <= int(self.script.get("polls_before_exit", 0)):
            return None
        if not self._produced:
            self._produced = True
            if self.script.get("produce", True):
                # argv 最后一个参数就是输出文件
                dst = Path(self.argv[-1])
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(b"fake-mp4")
        return int(self.script.get("exit_code", 0))


@pytest.fixture(autouse=True)
def _reset_fake():
    FakeFfmpeg.reset()
    yield
    FakeFfmpeg.reset()


@pytest.fixture()
def materials(tmp_path, monkeypatch):
    """素材根指向临时目录，并造好 clips/<组>/<片段>.mp4。"""
    root = tmp_path / "materials"
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(root))
    group = root / "clips" / "原片A_scenes"
    group.mkdir(parents=True)
    for name in ("a.mp4", "b.mp4", "c.mp4", "d.mp4"):
        (group / name).write_bytes(b"fake-video")
    (root / "output").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def _stub_tools(monkeypatch):
    """打桩外部工具：find_tool 返回假路径，probe_video_spec 返回固定规格。"""
    monkeypatch.setattr(mix_runner, "find_tool", lambda name: f"/fake/{name}")
    spec = {
        "width": 720, "height": 1280, "fps_num": 30, "fps_den": 1,
        "rotation": 0, "has_audio": True, "duration": 3.5,
    }
    monkeypatch.setattr(mix_runner, "probe_video_spec", lambda path: dict(spec))


def _make_job(db_session, tmp_path, *, opening, middle, ending, count=1, seed=7) -> MixJob:
    """直接落库一个 pending 任务（绕开服务层，runner 测试不碰 clip id 解析）。"""
    plans = plan_outputs(opening, middle, ending, count, seed)
    job = MixJob(
        status=MixJobStatus.PENDING,
        opening=list(opening),
        middle=list(middle),
        ending=list(ending),
        count=count,
        output_dir=str(tmp_path / "output" / "mix-test"),
        seed=seed,
        target={},
        total_outputs=count,
    )
    db_session.add(job)
    db_session.flush()
    for index, plan in enumerate(plans, start=1):
        db_session.add(
            MixJobItem(job_id=job.id, index=index, order=plan.order,
                       status=MixOutputStatus.PENDING)
        )
    db_session.commit()
    return job


def _make_runner(scripts=None, **overrides) -> MixRunner:
    script_iter = iter(scripts) if scripts is not None else None

    def factory(*args, **kwargs):
        script = next(script_iter) if script_iter is not None else {}
        return FakeFfmpeg(*args, script=script, **kwargs)

    options = {"tick_seconds": 0, "progress_seconds": 0}
    options.update(overrides)
    return MixRunner(
        session_factory=TestingSessionLocal,
        popen_factory=factory,
        **options,
    )


def _reload(db_session, job_id: int) -> MixJob:
    db_session.expire_all()
    return db_session.get(MixJob, job_id)


def REL(name: str) -> str:
    """素材的绝对路径 —— 任务里就是存这个（素材可以来自任意目录，没有共同的根）。"""
    return str(subdir(CLIPS) / "原片A_scenes" / f"{name}.mp4")


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


class TestPlanOutputs:
    def test_opening_and_ending_keep_user_order(self):
        plans = plan_outputs(["o1", "o2"], ["m1", "m2", "m3"], ["e1"], 2, 42)
        assert len(plans) == 2
        for plan in plans:
            assert plan.opening == ["o1", "o2"]
            assert plan.ending == ["e1"]
            assert plan.order == ["o1", "o2"] + plan.middle + ["e1"]

    def test_middle_is_shuffled_and_distinct_across_outputs(self):
        plans = plan_outputs(["o"], ["m1", "m2", "m3"], ["e"], 4, 1)
        middles = [tuple(p.middle) for p in plans]
        # 中间是同一集合但顺序互不相同
        assert all(sorted(m) == ["m1", "m2", "m3"] for m in middles)
        assert len(set(middles)) == 4

    def test_same_seed_reproduces_same_orders(self):
        a = plan_outputs(["o"], ["m1", "m2", "m3"], ["e"], 3, 99)
        b = plan_outputs(["o"], ["m1", "m2", "m3"], ["e"], 3, 99)
        assert [p.order for p in a] == [p.order for p in b]
        assert [p.seed for p in a] == [p.seed for p in b]

    def test_count_beyond_permutations_rejected(self):
        with pytest.raises(ValueError, match="最多只能排出 6 种"):
            plan_outputs(["o"], ["m1", "m2", "m3"], ["e"], 7, 1)

    def test_single_middle_clip_allows_only_one(self):
        assert middle_permutation_limit(1) == 1
        with pytest.raises(ValueError):
            plan_outputs(["o"], ["m1"], ["e"], 2, 1)


# --------------------------------------------------------------------------
# ffmpeg 命令构造
# --------------------------------------------------------------------------


class TestBuildCommands:
    SPEC = {"width": 720, "height": 1280, "fps_num": 30, "fps_den": 1}

    def test_normalize_filter_video_chain(self):
        f = build_normalize_filter(self.SPEC, 4.2, True)
        assert "scale=720:1280:force_original_aspect_ratio=decrease" in f
        assert "force_divisible_by=2" in f
        assert "pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black" in f
        assert "setsar=1" in f and "fps=30/1" in f and "format=yuv420p" in f
        assert "setpts=PTS-STARTPTS" in f

    def test_normalize_filter_audio_with_track(self):
        f = build_normalize_filter(self.SPEC, 4.2, True)
        assert "[0:a]aresample=48000" in f
        assert "atrim=start=0:end=4.2" in f
        assert "apad=whole_dur=4.2" in f

    def test_normalize_filter_audio_without_track(self):
        """无音轨：anullsrc 必须紧跟 atrim，否则编码永不结束。"""
        f = build_normalize_filter(self.SPEC, 4.2, False)
        assert "anullsrc=channel_layout=stereo:sample_rate=48000" in f
        assert f.index("anullsrc") < f.index("atrim")
        assert "[0:a]" not in f

    def test_normalize_argv(self):
        argv = build_normalize_argv(
            "/fake/ffmpeg", Path("/in.mp4"), Path("/out.mp4"),
            self.SPEC, 4.2, True, Path("/progress.txt"),
        )
        assert argv[0] == "/fake/ffmpeg"
        assert "-filter_complex" in argv
        joined = " ".join(argv)
        assert "-map [v]" in joined and "-map [a]" in joined
        assert "-c:v libx264" in joined
        assert "-movflags +faststart" in joined
        assert "-progress /progress.txt" in joined
        assert argv[-1] == "/out.mp4"

    def test_concat_list_escapes_single_quotes(self, tmp_path):
        list_path = tmp_path / "list.txt"
        write_concat_list(list_path, [Path("/a/it's.mp4"), Path("/b/正常.mp4")])
        content = list_path.read_text(encoding="utf-8")
        assert "file '/a/it'\\''s.mp4'" in content
        assert "file '/b/正常.mp4'" in content

    def test_concat_argv(self):
        argv = build_concat_argv(
            "/fake/ffmpeg", Path("/list.txt"), Path("/out.mp4"), Path("/p.txt")
        )
        joined = " ".join(argv)
        assert "-f concat -safe 0" in joined
        assert "-c:v copy" in joined
        assert "-c:a aac" in joined  # 音频重编码：copy 拼接会产生间隙/爆音
        assert argv[-1] == "/out.mp4"

    def test_read_ffmpeg_progress(self, tmp_path):
        p = tmp_path / "progress.txt"
        p.write_text("out_time_us=1000000\nout_time_us=2500000\n", encoding="utf-8")
        assert read_ffmpeg_progress_us(p) == 2500000
        assert read_ffmpeg_progress_us(tmp_path / "missing.txt") is None

    def test_probe_environment(self, materials):
        env = probe_environment()
        assert env["ready"] is True
        names = {d["name"] for d in env["dependencies"]}
        assert names == {"ffmpeg", "ffprobe"}
        # 「添加素材目录」选择器默认落在镜头切片目录上（存在时才落在那儿）
        assert env["default_source_dir"] == str(subdir(CLIPS))

    def test_probe_environment_without_clips_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(tmp_path / "空的"))
        env = probe_environment()
        assert env["default_source_dir"] == str(tmp_path / "空的")


class TestResolveClipPath:
    def test_absolute_path_used_as_is(self, tmp_path):
        raw = str(tmp_path / "别的目录" / "x.mp4")
        assert resolve_clip_path(raw) == Path(raw)

    def test_legacy_relative_path_falls_back_to_materials_root(self, materials):
        """改版前的历史任务存的是相对路径，不能一执行就变成「素材不存在」。"""
        path = resolve_clip_path("clips/原片A_scenes/a.mp4")
        assert path == subdir(CLIPS) / "原片A_scenes" / "a.mp4"
        assert path.is_file()


# --------------------------------------------------------------------------
# run_job 全流程
# --------------------------------------------------------------------------


class TestRunJob:
    def test_success_two_outputs(self, db_session, materials, tmp_path):
        """2 条成片：每个片段只归一化一次（4 次），每条成片各拼一次（2 次）。"""
        job = _make_job(
            db_session, tmp_path,
            opening=[REL("a")], middle=[REL("b"), REL("c")], ending=[REL("d")],
            count=2,
        )
        runner = _make_runner()
        assert runner.run_job(job.id) is True

        job = _reload(db_session, job.id)
        assert job.status == MixJobStatus.SUCCESS
        assert job.completed_outputs == 2
        assert job.done_clips == 4 and job.total_clips == 4
        assert job.target == {"width": 720, "height": 1280, "fps_num": 30, "fps_den": 1}
        # 归一化 4 次（去重并集）+ 拼接 2 次 = 6 次 ffmpeg
        assert len(FakeFfmpeg.instances) == 6
        for item in job.outputs:
            assert item.status == MixOutputStatus.SUCCESS
            assert Path(item.output_path).is_file()
            assert item.output_name == f"{item.index:02d}.mp4"
            assert item.duration_seconds == 14.0  # 4 段 × 3.5 秒
            assert item.size_bytes > 0
        # 两条成片的中间段顺序必须不同，开头结尾一致
        o1, o2 = job.outputs[0].order, job.outputs[1].order
        assert o1[0] == o2[0] and o1[-1] == o2[-1]
        assert o1 != o2
        # 任务成功 → 临时目录整个删除
        assert not (materials / "output" / ".tmp" / f"mix_{job.id}").exists()

    def test_normalize_failure_fails_affected_outputs(self, db_session, materials, tmp_path):
        """某片段归一化失败 → 所有包含它的成片都失败（本场景即全部）。"""
        job = _make_job(
            db_session, tmp_path,
            opening=[REL("a")], middle=[REL("b"), REL("c")], ending=[REL("d")],
            count=1,
        )
        # 4 次归一化：第 2 个片段失败（exit 1 且不产出文件）
        scripts = [{}, {"exit_code": 1, "produce": False}, {}, {}]
        runner = _make_runner(scripts=scripts)
        runner.run_job(job.id)

        job = _reload(db_session, job.id)
        assert job.status == MixJobStatus.FAILED
        assert job.failed_outputs == 1
        assert job.outputs[0].status == MixOutputStatus.FAILED
        assert "归一化失败" in job.outputs[0].error_message
        # 失败 → 保留 ffmpeg 日志目录，删掉 norm/ 省磁盘
        tmp_dir = materials / "output" / ".tmp" / f"mix_{job.id}"
        assert tmp_dir.exists()
        assert list(tmp_dir.glob("ffmpeg_norm_*.log"))

    def test_concat_failure_yields_partial(self, db_session, materials, tmp_path):
        """归一化全部成功、其中一条拼接失败 → 任务 partial。"""
        job = _make_job(
            db_session, tmp_path,
            opening=[REL("a")], middle=[REL("b"), REL("c")], ending=[REL("d")],
            count=2,
        )
        # 4 次归一化成功 + 拼接：第 1 条成功、第 2 条失败
        scripts = [{}, {}, {}, {}, {}, {"exit_code": 1, "produce": False}]
        runner = _make_runner(scripts=scripts)
        runner.run_job(job.id)

        job = _reload(db_session, job.id)
        assert job.status == MixJobStatus.PARTIAL
        assert job.outputs[0].status == MixOutputStatus.SUCCESS
        assert job.outputs[1].status == MixOutputStatus.FAILED
        # 失败的 partial 文件必须被清掉，半成品永不以成片名出现
        out_dir = Path(job.output_dir)
        assert not list(out_dir.glob("*.partial.mp4"))

    def test_cancel_during_concat_skips_rest(self, db_session, materials, tmp_path):
        """拼接第 1 条时取消：当前条 skipped，剩余条也 skipped。"""
        job = _make_job(
            db_session, tmp_path,
            opening=[REL("a")], middle=[REL("b"), REL("c")], ending=[REL("d")],
            count=2,
        )

        def cancel_on_poll(proc, poll_count):
            # 第 5 次进程（第 1 次拼接）第一次 poll 时把任务置为 cancelled，
            # 模拟用户在页面上点了取消
            if len(FakeFfmpeg.instances) == 5 and poll_count == 1:
                with TestingSessionLocal() as db:
                    db.query(MixJob).filter_by(id=job.id).update(
                        {"status": MixJobStatus.CANCELLED}
                    )
                    db.commit()

        scripts = [{}, {}, {}, {}, {"hang": True, "on_poll": cancel_on_poll}]
        runner = _make_runner(scripts=scripts)
        runner.run_job(job.id)

        job = _reload(db_session, job.id)
        assert job.status == MixJobStatus.CANCELLED
        assert job.outputs[0].status == MixOutputStatus.SKIPPED
        assert job.outputs[1].status == MixOutputStatus.SKIPPED
        # 取消 → 临时目录整个删除
        assert not (materials / "output" / ".tmp" / f"mix_{job.id}").exists()

    def test_missing_clip_fails_whole_job(self, db_session, materials, tmp_path):
        """素材被挪走：准备阶段就失败，不产出不报错乱码。"""
        job = _make_job(
            db_session, tmp_path,
            opening=[REL("a")], middle=[REL("b"), REL("c")], ending=[REL("ghost")],
            count=1,
        )
        runner = _make_runner()
        runner.run_job(job.id)

        job = _reload(db_session, job.id)
        assert job.status == MixJobStatus.FAILED
        assert "素材文件不存在" in job.outputs[0].error_message
        # 一次 ffmpeg 都不该起
        assert FakeFfmpeg.instances == []

    def test_legacy_relative_paths_still_run(self, db_session, materials, tmp_path):
        """改版前创建、还没跑的任务存的是相对路径，也得照样能执行。"""
        legacy = lambda name: f"clips/原片A_scenes/{name}.mp4"  # noqa: E731
        job = _make_job(
            db_session, tmp_path,
            opening=[legacy("a")], middle=[legacy("b"), legacy("c")], ending=[legacy("d")],
        )
        runner = _make_runner()
        assert runner.run_job(job.id) is True
        assert _reload(db_session, job.id).status == MixJobStatus.SUCCESS

    def test_claim_only_pending(self, db_session, materials, tmp_path):
        """非 pending 的任务 run_job 直接返回 False（并发抢锁的天然防重）。"""
        job = _make_job(
            db_session, tmp_path,
            opening=[REL("a")], middle=[REL("b"), REL("c")], ending=[REL("d")],
        )
        job.status = MixJobStatus.CANCELLED
        db_session.commit()
        runner = _make_runner()
        assert runner.run_job(job.id) is False
        assert FakeFfmpeg.instances == []


# --------------------------------------------------------------------------
# 孤儿回收
# --------------------------------------------------------------------------


class TestRecover:
    def test_recovers_running_job(self, db_session, tmp_path):
        job = _make_job(
            db_session, tmp_path,
            opening=[REL("a")], middle=[REL("b"), REL("c")], ending=[REL("d")],
        )
        job.status = MixJobStatus.RUNNING
        job.child_pid = 4_000_000  # 必然不存在的 PID：校验失败，不会误杀
        db_session.commit()

        recovered = recover_interrupted_jobs(session_factory=TestingSessionLocal)
        assert recovered == 1

        job = _reload(db_session, job.id)
        assert job.status == MixJobStatus.FAILED
        assert job.error_message == "服务重启，任务中断"
        assert job.child_pid is None
        assert job.outputs[0].status == MixOutputStatus.SKIPPED

    def test_pending_jobs_untouched(self, db_session, tmp_path):
        job = _make_job(
            db_session, tmp_path,
            opening=[REL("a")], middle=[REL("b"), REL("c")], ending=[REL("d")],
        )
        recovered = recover_interrupted_jobs(session_factory=TestingSessionLocal)
        assert recovered == 0
        assert _reload(db_session, job.id).status == MixJobStatus.PENDING
