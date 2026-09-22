"""换背景执行层（background_runner.py）测试。

**不起线程、不读真图片、不跑真算法**：`session_factory` 换成测试会话工厂，
`render_fn` / `load_page_fn` 换成假实现，整个流程在主线程同步跑完。真算法
在 test_cutout.py 里单独测 —— 两边混在一起的话，一个算法参数改动会让这里
几十条与算法无关的断言一起红，排查成本远高于收益。

这里要钉的是**执行语义**，也就是「算法正确但流程写错」那一类 bug：
- 一张失败不能中断整批（一批 200 张里有一张过曝是常态）；
- 取消后剩余张标 skipped 且已产出的图保留；
- 服务停止与用户取消的原因必须分开写；
- 重启回收要把 running 判 failed、pending 判 skipped（否则详情页会出现
  「任务已失败，里面一堆排队中」的自相矛盾状态）；
- 判成功的唯一依据是「无异常 + 产物存在且非空」—— 这里没有退出码可交叉验证。
"""

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.core.config import settings
from app.models.background_job import (
    BackgroundJob,
    BackgroundJobItem,
    BackgroundJobItemStatus,
    BackgroundJobStatus,
)
from app.schemas.background_job import BackgroundJobCreate
from app.services.background_job_service import BackgroundJobService
from app.services.background_runner import (
    BackgroundRunner,
    _jsonable,
    claim_next_pending_id,
    recover_interrupted_jobs,
)
from app.services.cutout import CutoutError
from tests.conftest import TestingSessionLocal

DEFAULT_NAMES = ("羊1.png", "羊2.png", "羊3.png")


@pytest.fixture(autouse=True)
def _isolate_materials(tmp_path, monkeypatch):
    """产物默认落在 materials/background/：不隔离会写进开发机的真实目录。"""
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(tmp_path / "materials"))


# --------------------------------------------------------------------------
# 夹具与工厂
# --------------------------------------------------------------------------


def _make_source(tmp_path: Path, names=DEFAULT_NAMES) -> Path:
    """造一个含假原图的输入目录（内容无所谓，服务层只按名字枚举）。"""
    source = tmp_path / "原图"
    source.mkdir(exist_ok=True)
    for name in names:
        (source / name).write_bytes(b"fake-image")
    return source


def _make_job(db_session, tmp_path: Path, names=DEFAULT_NAMES) -> BackgroundJob:
    """建一条排队中的换背景任务（真服务层，走真枚举与真路径分配）。"""
    source = _make_source(tmp_path, names)
    background = tmp_path / "背景.png"
    background.write_bytes(b"fake-bg")
    payload = BackgroundJobCreate(
        input_path=str(source), background_path=str(background)
    )
    return BackgroundJobService(db_session).create_job(payload)


def _fake_render(size=(8, 6), stats=None):
    """造一个假算法：返回一张真 PIL 图 + 假诊断。

    返回真 PIL 图而不是桩对象，是为了让 `_write_png` 走真路径（原子替换、
    真 PNG 编码、真 stat 大小）—— 那正是这里要覆盖的代码。
    """

    def render(source: Path, page, params, *, max_pixels):
        payload = {"source": source.name, "warnings": []}
        payload.update(stats or {})
        return Image.new("RGB", size, (10, 20, 30)), payload

    return render


def _fake_load_page(size=(40, 30)):
    def load(path: Path, *, max_pixels):
        return Image.new("RGB", size, (0, 0, 255))

    return load


def _make_runner(render_fn=None, load_page_fn=None, **overrides) -> BackgroundRunner:
    """构造一个连到测试库、用假算法的执行器。"""
    options = {
        "session_factory": TestingSessionLocal,
        "render_fn": render_fn or _fake_render(),
        "load_page_fn": load_page_fn or _fake_load_page(),
        "max_pixels": 30_000_000,
    }
    options.update(overrides)
    return BackgroundRunner(**options)


def _refresh(db_session, job: BackgroundJob) -> BackgroundJob:
    """丢弃会话缓存重新读库（runner 用的是另一条会话）。"""
    db_session.expire_all()
    return db_session.get(BackgroundJob, job.id)


def _items(db_session, job: BackgroundJob) -> list[BackgroundJobItem]:
    """按序号返回任务的条目（已刷新）。"""
    return list(_refresh(db_session, job).items)


# --------------------------------------------------------------------------
# 顺利跑完
# --------------------------------------------------------------------------


class TestHappyPath:
    """全部成功：状态、计数、产物、诊断。"""

    def test_all_items_succeed(self, db_session, tmp_path):
        job = _make_job(db_session, tmp_path)
        assert _make_runner().run_job(job.id) is True

        fresh = _refresh(db_session, job)
        assert fresh.status == BackgroundJobStatus.SUCCESS
        assert fresh.completed_images == 3
        assert fresh.failed_images == 0
        assert fresh.skipped_images == 0
        assert fresh.error_message == ""
        assert fresh.started_at is not None and fresh.finished_at is not None

    def test_every_artifact_is_a_readable_png(self, db_session, tmp_path):
        """产物必须是能打开的 PNG —— 「文件存在」不等于「图能看」。"""
        job = _make_job(db_session, tmp_path)
        _make_runner().run_job(job.id)

        for item in _items(db_session, job):
            path = Path(item.output_path)
            assert path.is_file()
            assert item.size_bytes == path.stat().st_size > 0
            with Image.open(path) as image:
                assert image.format == "PNG"
                assert image.size == (item.width, item.height) == (8, 6)

    def test_artifacts_land_in_the_task_directory(self, db_session, tmp_path):
        """产物落在任务自己的 background-<时间戳>/ 里（删任务才能整棵清干净）。"""
        job = _make_job(db_session, tmp_path)
        _make_runner().run_job(job.id)

        for item in _items(db_session, job):
            assert Path(item.output_path).parent == Path(job.output_dir)

    def test_no_part_file_is_left_behind(self, db_session, tmp_path):
        """原子替换的中间文件不能留在产物目录里。"""
        job = _make_job(db_session, tmp_path)
        _make_runner().run_job(job.id)

        residue = list(Path(job.output_dir).glob("*.part"))
        assert residue == []

    def test_stats_are_persisted(self, db_session, tmp_path):
        job = _make_job(db_session, tmp_path)
        _make_runner(render_fn=_fake_render(stats={"paper": 240, "ink_luma": 10})).run_job(job.id)

        first = _items(db_session, job)[0]
        assert first.stats["paper"] == 240
        assert first.stats["ink_luma"] == 10
        assert first.stats["source"] == "羊1.png"

    def test_current_progress_is_cleared_on_finish(self, db_session, tmp_path):
        """任务结束后不能再留着「当前这张」的实时进度（页面会显示成永远在跑）。"""
        job = _make_job(db_session, tmp_path)
        _make_runner().run_job(job.id)

        fresh = _refresh(db_session, job)
        assert fresh.current_image == ""
        assert fresh.current_elapsed_seconds == 0

    def test_item_timestamps_are_filled(self, db_session, tmp_path):
        job = _make_job(db_session, tmp_path)
        _make_runner().run_job(job.id)

        for item in _items(db_session, job):
            assert item.status == BackgroundJobItemStatus.SUCCESS
            assert item.started_at is not None and item.finished_at is not None
            assert item.elapsed_seconds >= 0
            assert item.error_message == ""


# --------------------------------------------------------------------------
# 部分失败：一张失败不能中断整批
# --------------------------------------------------------------------------


class TestPartialFailure:
    def _failing_on(self, bad_name: str, exc: Exception):
        def render(source: Path, page, params, *, max_pixels):
            if source.name == bad_name:
                raise exc
            return Image.new("RGB", (8, 6), (10, 20, 30)), {"source": source.name}

        return render

    def test_cutout_error_does_not_stop_the_batch(self, db_session, tmp_path):
        """过曝/损坏的一张判失败，其余照常产出（这是本功能最常见的失败形态）。"""
        job = _make_job(db_session, tmp_path)
        runner = _make_runner(
            render_fn=self._failing_on("羊2.png", CutoutError("没有比背景暗的主体"))
        )
        assert runner.run_job(job.id) is True

        fresh = _refresh(db_session, job)
        assert fresh.status == BackgroundJobStatus.PARTIAL
        assert (fresh.completed_images, fresh.failed_images) == (3, 1)
        assert "羊2.png" in fresh.error_message
        assert "没有比背景暗的主体" in fresh.error_message

        items = {item.source_name: item for item in fresh.items}
        assert items["羊1.png"].status == BackgroundJobItemStatus.SUCCESS
        assert items["羊3.png"].status == BackgroundJobItemStatus.SUCCESS
        assert items["羊2.png"].status == BackgroundJobItemStatus.FAILED
        # 失败的那张不能留下半截产物
        assert not Path(items["羊2.png"].output_path).exists()
        # 成功的两张确实落盘了
        assert Path(items["羊1.png"].output_path).is_file()

    def test_unexpected_exception_is_contained_and_marked(self, db_session, tmp_path):
        """算法之外的意外（比如 Pillow 抛的）也不能带走整个批次。"""
        job = _make_job(db_session, tmp_path)
        runner = _make_runner(
            render_fn=self._failing_on("羊1.png", ValueError("幕布尺寸对不上"))
        )
        runner.run_job(job.id)

        first = _items(db_session, job)[0]
        assert first.status == BackgroundJobItemStatus.FAILED
        assert first.error_message.startswith("处理异常：")
        assert "幕布尺寸对不上" in first.error_message
        assert _refresh(db_session, job).status == BackgroundJobStatus.PARTIAL

    def test_all_failed_marks_the_job_failed(self, db_session, tmp_path):
        job = _make_job(db_session, tmp_path)

        def always_fail(source, page, params, *, max_pixels):
            raise CutoutError("这张图超过单张上限")

        _make_runner(render_fn=always_fail).run_job(job.id)

        fresh = _refresh(db_session, job)
        assert fresh.status == BackgroundJobStatus.FAILED
        assert fresh.failed_images == 3
        # 错误汇总只取前三条，且带文件名
        assert "羊1.png" in fresh.error_message

    def test_error_summary_is_bounded(self, db_session, tmp_path):
        """一批 200 张全失败时，error_message 不能长到把列表接口撑爆。"""
        names = tuple(f"图{i}.png" for i in range(1, 6))
        job = _make_job(db_session, tmp_path, names=names)

        def always_fail(source, page, params, *, max_pixels):
            raise CutoutError(f"{source.name} 处理不了")

        _make_runner(render_fn=always_fail).run_job(job.id)

        message = _refresh(db_session, job).error_message
        assert len(message) <= 500
        # 只列前三张
        assert message.count("；") == 2

    def test_long_error_is_truncated_per_item(self, db_session, tmp_path):
        job = _make_job(db_session, tmp_path)

        def always_fail(source, page, params, *, max_pixels):
            raise CutoutError("呀" * 2000)

        _make_runner(render_fn=always_fail).run_job(job.id)

        for item in _items(db_session, job):
            assert len(item.error_message) <= 500


# --------------------------------------------------------------------------
# 取消与服务停止
# --------------------------------------------------------------------------


class TestCancel:
    def test_cancel_skips_the_rest_and_keeps_what_was_produced(self, db_session, tmp_path):
        """取消的语义：**当前这张算完就收手**，剩下的标 skipped，已产出的留在盘上。

        取消延迟上界 = 单张处理时间（没有子进程可杀），所以这里用「第一张跑完时
        把任务置成 cancelled」来模拟用户在第二张处理期间点了取消。
        """
        job = _make_job(db_session, tmp_path)

        def render_then_cancel(source, page, params, *, max_pixels):
            if source.name == "羊1.png":
                with TestingSessionLocal() as other:
                    other.get(BackgroundJob, job.id).status = BackgroundJobStatus.CANCELLED
                    other.commit()
            return Image.new("RGB", (8, 6), (10, 20, 30)), {}

        _make_runner(render_fn=render_then_cancel).run_job(job.id)

        fresh = _refresh(db_session, job)
        assert fresh.status == BackgroundJobStatus.CANCELLED
        assert fresh.skipped_images == 2
        assert fresh.failed_images == 0
        assert fresh.current_image == ""

        items = {item.source_name: item for item in fresh.items}
        assert items["羊1.png"].status == BackgroundJobItemStatus.SUCCESS
        assert Path(items["羊1.png"].output_path).is_file()
        for name in ("羊2.png", "羊3.png"):
            assert items[name].status == BackgroundJobItemStatus.SKIPPED
            assert items[name].error_message == "任务已取消"
            assert not Path(items[name].output_path).exists()

    def test_cancelled_job_is_not_reclaimed(self, db_session, tmp_path):
        """取消过的任务不会被认领（条件 UPDATE 只看 pending）。"""
        job = _make_job(db_session, tmp_path)
        BackgroundJobService(db_session).cancel_job(job.id)

        assert _make_runner().run_job(job.id) is False
        assert _refresh(db_session, job).status == BackgroundJobStatus.CANCELLED

    def test_cancel_wins_over_stop_event(self, db_session, tmp_path):
        """两个信号同时置位时以「取消」为准 —— 用户点过取消就不该看到「服务停止」。"""
        job = _make_job(db_session, tmp_path)
        BackgroundJobService(db_session).cancel_job(job.id)

        class _Set:
            def is_set(self) -> bool:
                return True

        # 任务已是 cancelled，认领失败直接返回，不会走到逐张循环 —— 这正是期望：
        # 没被认领的任务不需要任何条目级的标记
        assert _make_runner().run_job(job.id, stop_event=_Set()) is False


class TestStopEvent:
    def test_stop_event_skips_the_rest_with_its_own_reason(self, db_session, tmp_path):
        """服务停止的原因文案必须与「任务已取消」区分开。

        两种原因混着写，用户会对一次热重启一头雾水 —— 明明没人点取消。
        """
        job = _make_job(db_session, tmp_path)

        class _Stopper:
            """第一张跑完后置位，模拟服务在第二张之前开始关停。"""

            def __init__(self) -> None:
                self.flag = False

            def is_set(self) -> bool:
                return self.flag

        stopper = _Stopper()

        def render(source, page, params, *, max_pixels):
            stopper.flag = True
            return Image.new("RGB", (8, 6), (10, 20, 30)), {}

        _make_runner(render_fn=render).run_job(job.id, stop_event=stopper)

        fresh = _refresh(db_session, job)
        # 一张成功 + 两张跳过 → partial（有产出就不算全败）
        assert fresh.status == BackgroundJobStatus.PARTIAL
        assert fresh.skipped_images == 2

        items = {item.source_name: item for item in fresh.items}
        assert items["羊1.png"].status == BackgroundJobItemStatus.SUCCESS
        for name in ("羊2.png", "羊3.png"):
            assert items[name].status == BackgroundJobItemStatus.SKIPPED
            assert items[name].error_message == "服务停止或重启，未执行"


# --------------------------------------------------------------------------
# 整条任务级的前置条件不满足
# --------------------------------------------------------------------------


class TestBackgroundUnreadable:
    """背景图在排队期间被删/损坏：整条任务失败，而不是每张各失败一次。"""

    def _job(self, db_session, tmp_path):
        return _make_job(db_session, tmp_path)

    def test_missing_background_fails_the_whole_job(self, db_session, tmp_path):
        job = self._job(db_session, tmp_path)

        def broken_page(path, *, max_pixels):
            raise CutoutError("找不到背景图：背景.png")

        _make_runner(load_page_fn=broken_page).run_job(job.id)

        fresh = _refresh(db_session, job)
        assert fresh.status == BackgroundJobStatus.FAILED
        assert "背景图读不出来" in fresh.error_message
        for item in fresh.items:
            assert item.status == BackgroundJobItemStatus.FAILED
            assert "背景图读不出来" in item.error_message

    def test_job_does_not_stay_running(self, db_session, tmp_path):
        """_fail_all 不落任务状态，必须靠 _finalize 收尾 —— 漏了会永远停在 running。"""
        job = self._job(db_session, tmp_path)

        def broken_page(path, *, max_pixels):
            raise CutoutError("读不出这张背景图")

        _make_runner(load_page_fn=broken_page).run_job(job.id)

        fresh = _refresh(db_session, job)
        assert fresh.status != BackgroundJobStatus.RUNNING
        assert fresh.finished_at is not None
        assert fresh.current_image == ""

    def test_background_is_read_once_for_the_whole_job(self, db_session, tmp_path):
        """背景图整条任务只解码一次：每张重读等于把同一张大图解码 N 遍。"""
        job = self._job(db_session, tmp_path)
        calls = []

        def counting_page(path, *, max_pixels):
            calls.append(path)
            return Image.new("RGB", (40, 30), (0, 0, 255))

        _make_runner(load_page_fn=counting_page).run_job(job.id)

        assert calls == [Path(job.background_path)]


# --------------------------------------------------------------------------
# 认领
# --------------------------------------------------------------------------


class TestClaim:
    def test_claim_returns_lowest_pending_id(self, db_session, tmp_path):
        first = _make_job(db_session, tmp_path)
        second = _make_job(db_session, tmp_path)

        assert claim_next_pending_id(TestingSessionLocal) == first.id
        assert claim_next_pending_id(TestingSessionLocal) != second.id  # 取的是最小 ID

    def test_claim_returns_none_when_queue_is_empty(self, db_session):
        assert claim_next_pending_id(TestingSessionLocal) is None

    def test_claim_skips_non_pending_jobs(self, db_session, tmp_path):
        job = _make_job(db_session, tmp_path)
        BackgroundJobService(db_session).cancel_job(job.id)
        assert claim_next_pending_id(TestingSessionLocal) is None

    def test_run_job_twice_returns_false_the_second_time(self, db_session, tmp_path):
        """条件 UPDATE 抢锁：同一任务不会被跑两遍。"""
        job = _make_job(db_session, tmp_path)
        runner = _make_runner()

        assert runner.run_job(job.id) is True
        assert runner.run_job(job.id) is False
        assert _refresh(db_session, job).completed_images == 3

    def test_run_job_on_unknown_id(self, db_session):
        assert _make_runner().run_job(999999) is False


# --------------------------------------------------------------------------
# 重启回收
# --------------------------------------------------------------------------


class TestRecoverInterrupted:
    def _interrupted(self, db_session, tmp_path) -> BackgroundJob:
        """造一条「跑到第二张时服务被杀」的任务。"""
        job = _make_job(db_session, tmp_path)
        job.status = BackgroundJobStatus.RUNNING
        job.current_image = "羊2.png"
        job.current_elapsed_seconds = 7
        job.items[0].status = BackgroundJobItemStatus.SUCCESS
        job.items[1].status = BackgroundJobItemStatus.RUNNING
        db_session.commit()
        return job

    def test_running_job_becomes_failed(self, db_session, tmp_path):
        job = self._interrupted(db_session, tmp_path)

        assert recover_interrupted_jobs(TestingSessionLocal) == 1

        fresh = _refresh(db_session, job)
        assert fresh.status == BackgroundJobStatus.FAILED
        assert "服务停止或重启" in fresh.error_message
        assert fresh.finished_at is not None
        assert fresh.current_image == ""
        assert fresh.current_elapsed_seconds == 0

    def test_items_are_settled_too(self, db_session, tmp_path):
        """只收任务、不收条目的话，详情页会出现「任务已失败，里面一堆排队中」。"""
        job = self._interrupted(db_session, tmp_path)
        recover_interrupted_jobs(TestingSessionLocal)

        items = {item.source_name: item for item in _items(db_session, job)}
        assert items["羊1.png"].status == BackgroundJobItemStatus.SUCCESS  # 已产出的不动
        assert items["羊2.png"].status == BackgroundJobItemStatus.FAILED
        assert items["羊2.png"].error_message == "服务停止或重启，任务中断（可直接重新发起）"
        assert items["羊3.png"].status == BackgroundJobItemStatus.SKIPPED
        assert items["羊3.png"].error_message == "服务停止或重启，未执行"

    def test_pending_jobs_are_left_alone(self, db_session, tmp_path):
        """pending 任务由 DB 队列自动接手，恢复代码不许动它。"""
        job = _make_job(db_session, tmp_path)

        assert recover_interrupted_jobs(TestingSessionLocal) == 0
        assert _refresh(db_session, job).status == BackgroundJobStatus.PENDING

    def test_terminal_jobs_are_left_alone(self, db_session, tmp_path):
        job = _make_job(db_session, tmp_path)
        _make_runner().run_job(job.id)

        assert recover_interrupted_jobs(TestingSessionLocal) == 0
        assert _refresh(db_session, job).status == BackgroundJobStatus.SUCCESS


# --------------------------------------------------------------------------
# 诊断统计的 JSON 边界
# --------------------------------------------------------------------------


class TestJsonable:
    """stats 直接进 JSON 列：混进 numpy 标量会在 commit 时炸成「假失败」。"""

    def test_numpy_float_is_not_json_serializable(self):
        """前提：这就是必须兜一道的原因。"""
        with pytest.raises(TypeError):
            json.dumps({"v": np.float32(1.5)})

    def test_numpy_scalars_are_unwrapped(self):
        cleaned = _jsonable({"a": np.float32(1.5), "b": np.int64(3)})
        assert cleaned == {"a": 1.5, "b": 3}
        assert type(cleaned["a"]) is float
        json.dumps(cleaned)   # 不抛即通过

    def test_numbers_keep_their_numeric_type(self):
        """**不能**退化成字符串：`"0.42"` 不崩，但前端的比较与百分比换算全会走偏。"""
        cleaned = _jsonable({"ratio": np.float32(0.42), "count": np.int32(7)})
        assert cleaned == {"ratio": pytest.approx(0.42), "count": 7}
        assert type(cleaned["count"]) is int
        assert type(cleaned["ratio"]) is float

    def test_numpy_bool_becomes_a_real_bool(self):
        """`np.bool_` 不是 `bool` 的子类：不退化成 `"True"` 字符串，前端才能当真值用。"""
        cleaned = _jsonable({"gate": np.bool_(True)})
        assert cleaned["gate"] is True

    def test_arrays_become_lists(self):
        """数组走 tolist()：`str(array)` 会得到 "[1 2 3]" 这种带奇怪空格的东西。"""
        cleaned = _jsonable({"bbox": np.array([120, 80, 219, 159])})
        assert cleaned["bbox"] == [120, 80, 219, 159]

    def test_nested_containers_are_walked(self):
        cleaned = _jsonable({"warnings": ["a"], "box": (np.float32(1), 2)})
        assert cleaned == {"warnings": ["a"], "box": [1.0, 2]}

    def test_unknown_objects_degrade_to_string(self):
        """真正认不出来的类型才降级成字符串 —— 诊断数字而已，不值得判这张图失败。"""

        class _Opaque:
            def __str__(self) -> str:
                return "某个认不出的东西"

        cleaned = _jsonable({"weird": _Opaque()})
        assert cleaned == {"weird": "某个认不出的东西"}
        json.dumps(cleaned)

    def test_none_and_bools_survive(self):
        cleaned = _jsonable({"a": None, "b": True})
        assert cleaned == {"a": None, "b": True}

    def test_bad_stats_do_not_fail_a_successful_render(self, db_session, tmp_path):
        """端到端回归：算法返回带 numpy 标量的 stats，任务照样成功、产物照样落盘。"""
        job = _make_job(db_session, tmp_path)
        runner = _make_runner(
            render_fn=_fake_render(stats={"paper": np.float32(240.5), "n": np.int64(2)})
        )
        runner.run_job(job.id)

        fresh = _refresh(db_session, job)
        assert fresh.status == BackgroundJobStatus.SUCCESS
        first = fresh.items[0]
        assert first.status == BackgroundJobItemStatus.SUCCESS
        assert first.stats["paper"] == 240.5
        assert Path(first.output_path).is_file()


# --------------------------------------------------------------------------
# 落盘失败与空文件
# --------------------------------------------------------------------------


class _StubImage:
    """一个能报告尺寸、但 save() 行为可由测试指定的假图。"""

    def __init__(self, *, width=8, height=6, mode="empty") -> None:
        self.width = width
        self.height = height
        self.mode = mode

    def save(self, path, *, format) -> None:
        if self.mode == "empty":
            Path(path).write_bytes(b"")           # 落了盘但是个空文件
        elif self.mode == "raises":
            raise OSError("No space left on device")


class TestWriteFailures:
    def test_empty_artifact_counts_as_failure(self, db_session, tmp_path):
        """没有子进程、没有退出码，「产物非空」是唯一的判据。"""
        job = _make_job(db_session, tmp_path)

        def render(source, page, params, *, max_pixels):
            return _StubImage(mode="empty"), {}

        _make_runner(render_fn=render).run_job(job.id)

        item = _items(db_session, job)[0]
        assert item.status == BackgroundJobItemStatus.FAILED
        assert "空文件" in item.error_message

    def test_write_error_is_reported_and_leaves_no_residue(self, db_session, tmp_path):
        job = _make_job(db_session, tmp_path)

        def render(source, page, params, *, max_pixels):
            return _StubImage(mode="raises"), {}

        _make_runner(render_fn=render).run_job(job.id)

        items = _items(db_session, job)
        assert items[0].status == BackgroundJobItemStatus.FAILED
        assert "写出产物失败" in items[0].error_message
        # 临时文件被清掉，目标路径上也没有半截产物
        assert list(Path(job.output_dir).glob("*.part")) == []
        assert not Path(items[0].output_path).exists()

    def test_unwritable_output_dir_is_reported(self, db_session, tmp_path):
        """输出目录建不出来（父路径被一个文件占住）→ 该张失败，不崩）。"""
        job = _make_job(db_session, tmp_path)
        item = job.items[0]
        # 把产物路径的父目录换成同名文件，mkdir 必然失败
        blocked = tmp_path / "blocked"
        blocked.write_bytes(b"i am a file")
        item.output_path = str(blocked / "x" / "羊1.png")
        db_session.commit()

        _make_runner().run_job(job.id)

        first = _items(db_session, job)[0]
        assert first.status == BackgroundJobItemStatus.FAILED
        assert "创建输出目录失败" in first.error_message
