"""换背景端到端：**真算法**跑完整条任务线（队列 → 执行 → 落盘 → 落库）。

为什么单独一个文件：`test_cutout.py` 测算法但不碰 DB，`test_background_runner.py`
测执行语义但注入假算法 —— 两份都把「不混在一起」写进了各自的 docstring，理由是
一个算法参数改动不该让几十条与算法无关的断言一起红。

代价是**两条缝没人守**：真算法与执行器的接缝（`render_one` 的返回值形状、
stats 能不能过 JSON 序列化）、以及算法与存储层的接缝（产物真写成了 PNG 没有、
尺寸对不对）。这里用一份**合成图**把它钉住 —— 不依赖任何仓库外的样例文件，
所以永远跑得起来；算法本身的正确性仍然只在 `test_cutout.py` 里测。
"""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.core.config import settings
from app.models.background_job import BackgroundJob, BackgroundJobStatus
from app.schemas.background_job import BackgroundJobCreate
from app.services.background_job_service import BackgroundJobService
from app.services.background_runner import BackgroundRunner
from tests.conftest import TestingSessionLocal


@pytest.fixture(autouse=True)
def _isolate_materials(tmp_path, monkeypatch):
    """产物默认落在 materials/background/：不隔离会写进开发机的真实目录。"""
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(tmp_path / "materials"))


def _make_hand_drawn(path: Path, size=(200, 160)) -> Path:
    """造一张「浅纸 + 深色线条」的合成手绘：算法的主场。"""
    paper = np.full((size[1], size[0], 3), 240, dtype=np.uint8)
    # 一段粗笔画，落在画面中上部，四周留白（位置门要靠它圈包围盒）
    paper[60:80, 40:160] = 20
    paper[100:120, 40:160] = 20
    Image.fromarray(paper, "RGB").save(path)
    return path


def _make_background(path: Path, size=(300, 240), color=(0, 0, 255)) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def _run(db_session, tmp_path: Path) -> BackgroundJob:
    """建一条真任务并用**真算法**跑完，返回刷新后的任务。"""
    source_dir = tmp_path / "原图"
    source_dir.mkdir()
    _make_hand_drawn(source_dir / "手绘.png")
    background = _make_background(tmp_path / "背景.png")

    job = BackgroundJobService(db_session).create_job(
        BackgroundJobCreate(input_path=str(source_dir), background_path=str(background))
    )

    # 不传 render_fn / load_page_fn 就是真算法 + 真读图（这是本文件与
    # test_background_runner.py 的唯一差别）
    runner = BackgroundRunner(
        session_factory=TestingSessionLocal, max_pixels=settings.BACKGROUND_MAX_PIXELS
    )
    assert runner.run_job(job.id) is True

    db_session.expire_all()
    return db_session.get(BackgroundJob, job.id)


def test_real_algorithm_runs_end_to_end(db_session, tmp_path):
    """真算法走完整条任务线：任务成功、产物是真 PNG、尺寸等于背景。"""
    job = _run(db_session, tmp_path)

    assert job.status == BackgroundJobStatus.SUCCESS
    assert (job.completed_images, job.failed_images) == (1, 0)

    item = job.items[0]
    output = Path(item.output_path)
    assert output.is_file()
    assert item.size_bytes == output.stat().st_size > 0

    # 产物是换了底的整图：尺寸跟背景，不是原图尺寸
    with Image.open(output) as image:
        assert image.size == (300, 240)
        assert image.mode in {"RGB", "RGBA"}
        assert item.width == 300 and item.height == 240


def test_product_keeps_ink_and_takes_background(db_session, tmp_path):
    """合成结果要两头都占：线条处是墨色，四角是背景色。

    只断言「文件存在」的话，一张纯背景图也能过 —— 那就等于什么都没测。
    """
    job = _run(db_session, tmp_path)
    with Image.open(job.items[0].output_path) as image:
        pixels = np.asarray(image.convert("RGB"))

    corner = pixels[0, 0]
    assert corner[2] > 200  # 蓝：背景原样保留
    assert corner[0] < 60

    # 抠出的线条贴在正中：笔画在合成图里应当有一片明显偏暗的像素
    darker = (pixels.mean(axis=2) < 120).sum()
    assert darker > 100, "合成图里找不到墨色线条，抠图或贴合没生效"


def test_stats_survive_storage_and_serialization(db_session, tmp_path):
    """诊断 stats 要真的落进库里，且是数字不是字符串。

    曾经 `_jsonable` 会把 numpy 标量降级成字符串（`np.float32(0.42)` → `"0.42"`），
    数值语义悄悄没了。这里连着存储层一起钉住。
    """
    from app.services.background_runner import _jsonable

    job = _run(db_session, tmp_path)
    stats = job.items[0].stats

    assert stats["warnings"] == []
    assert isinstance(stats["ink_rgb"], list) and len(stats["ink_rgb"]) == 3
    assert stats["gate"] is True
    # 落库后再取出来（SQLite 存的是 TEXT）也必须是数字
    assert isinstance(_jsonable(stats["paper"]), (int, float))
    assert isinstance(stats["bbox"], list)
