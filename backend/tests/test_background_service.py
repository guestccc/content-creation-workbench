"""换背景任务服务层（background_job_service.py）测试。

只管「状态机 + 查询 + 创建时的枚举与路径分配」——算法与执行不在这里，见
test_cutout.py / test_background_runner.py。

创建任务时的校验是重点：这些错误必须在用户点「开始」的那一刻就报出来，
而不是排队几分钟后整条任务失败。
"""

from pathlib import Path

import pytest

from app.core.config import settings
from app.core.exceptions import BadRequestError, ConflictError, NotFoundError
from app.models.background_job import (
    BackgroundJobStatus,
    BackgroundJobItemStatus,
)
from app.models.crawl_job import CrawlJob
from app.schemas.background_job import BackgroundJobCreate, BackgroundJobParams
from app.schemas.common import JobRemarkUpdate
from app.services.background_job_service import BackgroundJobService


@pytest.fixture(autouse=True)
def _isolate_materials(tmp_path, monkeypatch):
    """把素材目录指到 tmp：产物默认落在 materials/background/，
    不隔离的话会写进开发机上的真实目录。"""
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(tmp_path / "materials"))
    return tmp_path / "materials"


@pytest.fixture()
def background_image(tmp_path) -> Path:
    """一张假背景图（内容无所谓，服务层不读像素）。"""
    path = tmp_path / "背景.png"
    path.write_bytes(b"fake-png")
    return path


@pytest.fixture()
def source_dir(tmp_path) -> Path:
    """一个含三张图片 + 两个应当被忽略的文件的输入目录。"""
    source = tmp_path / "原图"
    source.mkdir()
    for name in ("羊1.png", "羊2.PNG", "羊3.jpg"):
        (source / name).write_bytes(b"fake-image")
    (source / "说明.txt").write_text("t", encoding="utf-8")
    (source / "._羊1.png").write_bytes(b"appledouble")
    return source


def _payload(source_dir: Path, background: Path, **overrides) -> BackgroundJobCreate:
    data = {
        "input_path": str(source_dir),
        "background_path": str(background),
    }
    data.update(overrides)
    return BackgroundJobCreate(**data)


class TestCreate:
    """创建任务：枚举、校验、产物路径分配。"""

    def test_enumerates_images_only(self, db_session, source_dir, background_image):
        """按**图片**白名单枚举：文本与 ._ 开头的 AppleDouble 都不算。"""
        job = BackgroundJobService(db_session).create_job(
            _payload(source_dir, background_image)
        )
        names = [item.source_name for item in job.items]
        # 按路径小写排序：羊1.png / 羊2.PNG / 羊3.jpg
        assert names == ["羊1.png", "羊2.PNG", "羊3.jpg"]
        assert job.total_images == 3
        assert job.status == BackgroundJobStatus.PENDING
        assert all(i.status == BackgroundJobItemStatus.PENDING for i in job.items)

    def test_files_mode_keeps_only_checked(self, db_session, source_dir, background_image):
        """勾选模式：只处理勾上的那几张。"""
        job = BackgroundJobService(db_session).create_job(
            _payload(source_dir, background_image, files=["羊3.jpg", "羊1.png"])
        )
        assert [i.source_name for i in job.items] == ["羊1.png", "羊3.jpg"]
        assert job.files == ["羊3.jpg", "羊1.png"]

    def test_checked_file_must_exist(self, db_session, source_dir, background_image):
        with pytest.raises(BadRequestError, match="不存在"):
            BackgroundJobService(db_session).create_job(
                _payload(source_dir, background_image, files=["没有这张.png"])
            )

    def test_files_rejects_path_separators(self, source_dir, background_image):
        """勾选的名字会与输入目录拼接：允许分隔符就等于允许越出输入目录。"""
        with pytest.raises(ValueError, match="路径分隔符"):
            _payload(source_dir, background_image, files=["../秘密.png"])

    def test_output_paths_end_with_png(self, db_session, source_dir, background_image):
        """产物恒为 PNG：抠图结果带 alpha，jpg 存不了透明通道。"""
        job = BackgroundJobService(db_session).create_job(
            _payload(source_dir, background_image)
        )
        assert all(i.output_path.endswith(".png") for i in job.items)
        assert [Path(i.output_path).name for i in job.items] == [
            "羊1.png", "羊2.png", "羊3.png",
        ]

    def test_output_dir_is_a_fresh_timestamped_subdir(
        self, db_session, source_dir, background_image
    ):
        """每次任务独占一个 background-<时间戳>/，产物不互相覆盖。"""
        job = BackgroundJobService(db_session).create_job(
            _payload(source_dir, background_image)
        )
        out = Path(job.output_dir)
        assert out.is_dir()
        assert out.name.startswith(settings.BACKGROUND_OUTPUT_PREFIX)
        # materials/background/background-<时间戳>/：分段名 + 独占子目录
        assert out.parent.name == "background"
        assert out.parent.parent == Path(settings.SCENE_MATERIALS_DIR)

    def test_explicit_output_root_is_used(self, db_session, source_dir, background_image, tmp_path):
        root = tmp_path / "我的产物"
        root.mkdir()
        job = BackgroundJobService(db_session).create_job(
            _payload(source_dir, background_image, output_dir=str(root))
        )
        assert Path(job.output_dir).parent == root

    def test_missing_output_root_rejected(self, db_session, source_dir, background_image, tmp_path):
        with pytest.raises(BadRequestError, match="输出目录不存在"):
            BackgroundJobService(db_session).create_job(
                _payload(source_dir, background_image, output_dir=str(tmp_path / "没有"))
            )

    def test_missing_background_rejected(self, db_session, source_dir, tmp_path):
        with pytest.raises(BadRequestError, match="背景图不存在"):
            BackgroundJobService(db_session).create_job(
                _payload(source_dir, tmp_path / "没有.png")
            )

    def test_unsupported_background_extension_rejected(
        self, db_session, source_dir, tmp_path
    ):
        """背景图后缀不在算法白名单里 → 400（用户点的那一刻就知道）。"""
        weird = tmp_path / "背景.svg"
        weird.write_text("<svg/>", encoding="utf-8")
        with pytest.raises(BadRequestError, match="格式不受支持"):
            BackgroundJobService(db_session).create_job(
                _payload(source_dir, weird)
            )

    def test_directory_without_images_rejected(self, db_session, background_image, tmp_path):
        """空目录的文案必须说「图片」—— 对一个全是 PNG 的目录说「视频」纯属误导。"""
        empty = tmp_path / "只有文本"
        empty.mkdir()
        (empty / "说明.txt").write_text("t", encoding="utf-8")
        with pytest.raises(BadRequestError, match="没有可处理的图片文件"):
            BackgroundJobService(db_session).create_job(
                _payload(empty, background_image)
            )

    def test_params_are_snapshotted(self, db_session, source_dir, background_image):
        """参数在创建时快照进任务记录：之后改默认值不影响已建的任务。"""
        job = BackgroundJobService(db_session).create_job(
            _payload(
                source_dir,
                background_image,
                params=BackgroundJobParams(scale=0.5, pos="tl", warm=False),
            )
        )
        assert job.params["scale"] == 0.5
        assert job.params["pos"] == "tl"
        assert job.params["warm"] is False
        # 没显式给的字段也要落进快照，执行时不必再回头找默认值
        assert job.params["hi_frac"] == 0.90


class TestSourceProvenance:
    """来源字段：只记录，不产生约束。"""

    @staticmethod
    def _crawl_job(db_session):
        """造一条素材抓取任务行，给换背景任务当来源。"""
        crawl = CrawlJob(
            platform="xhs",
            crawler_type="search",
            login_type="qrcode",
            params={"keywords": ["保温杯"]},
        )
        db_session.add(crawl)
        db_session.commit()
        return crawl

    def test_source_is_snapshotted(self, db_session, source_dir, background_image):
        crawl = self._crawl_job(db_session)
        job = BackgroundJobService(db_session).create_job(
            _payload(
                source_dir,
                background_image,
                source_crawl_job_id=crawl.id,
                source_crawl_note_id="note-1",
            )
        )
        assert job.source_crawl_job_id == crawl.id
        assert job.source_crawl_note_id == "note-1"

    def test_no_source_means_empty_columns(self, db_session, source_dir, background_image):
        """自己挑目录建的任务：可空列是 NULL、非空列是空串（前端靠它判断）。"""
        job = BackgroundJobService(db_session).create_job(
            _payload(source_dir, background_image)
        )
        assert job.source_crawl_job_id is None
        assert job.source_crawl_note_id == ""

    def test_unknown_source_job_is_rejected_before_any_directory(
        self, db_session, source_dir, background_image
    ):
        """来源任务不存在 → BadRequestError，且**没有**留下输出目录。

        校验必须排在 mkdir 之前：晚一步，用户每点一次就多一个空的
        background-<时间戳>/ 目录。
        """
        with pytest.raises(BadRequestError, match="来源素材抓取任务不存在"):
            BackgroundJobService(db_session).create_job(
                _payload(
                    source_dir,
                    background_image,
                    source_crawl_job_id=999,
                    source_crawl_note_id="note-1",
                )
            )
        assert not Path(settings.SCENE_MATERIALS_DIR).exists()

    def test_note_id_alone_is_rejected_by_the_schema(self, source_dir, background_image):
        """成对校验在 schema 层：只给笔记 id 连 payload 都构造不出来。"""
        with pytest.raises(ValueError, match="来源素材抓取任务 id"):
            _payload(source_dir, background_image, source_crawl_note_id="note-1")


class TestParamsValidation:
    """参数范围在入口就钉死，别让算法安静地产出一张废图。"""

    def test_hi_must_exceed_lo(self):
        """纸的透明线必须高于墨的实心线，反了会把纸抠成墨。"""
        with pytest.raises(ValueError, match="必须大于"):
            BackgroundJobParams(hi_frac=0.1, lo_frac=0.9)

    def test_pos_accepts_keywords_and_coordinates(self):
        assert BackgroundJobParams(pos="tr").pos == "tr"
        assert BackgroundJobParams(pos="120, 40").pos == "120, 40"
        assert BackgroundJobParams(pos="").pos is None

    def test_pos_rejects_typos(self):
        """拼错的关键字会静默退回默认行为 —— 「点了没用还不报错」最难查。"""
        with pytest.raises(ValueError, match="贴合位置只能是"):
            BackgroundJobParams(pos="centre")

    def test_opacity_range(self):
        with pytest.raises(ValueError):
            BackgroundJobParams(opacity=2.0)

    def test_scale_must_be_positive(self):
        with pytest.raises(ValueError):
            BackgroundJobParams(scale=0)


class TestStateMachine:
    """状态流转：取消、备注、删除。"""

    def _job(self, db_session, source_dir, background_image):
        return BackgroundJobService(db_session).create_job(
            _payload(source_dir, background_image)
        )

    def test_cancel_pending_job(self, db_session, source_dir, background_image):
        job = self._job(db_session, source_dir, background_image)
        cancelled = BackgroundJobService(db_session).cancel_job(job.id)
        assert cancelled.status == BackgroundJobStatus.CANCELLED
        assert cancelled.finished_at is not None

    def test_cannot_cancel_terminal_job(self, db_session, source_dir, background_image):
        job = self._job(db_session, source_dir, background_image)
        service = BackgroundJobService(db_session)
        service.cancel_job(job.id)
        with pytest.raises(ConflictError, match="终态"):
            service.cancel_job(job.id)

    def test_cancel_unknown_job(self, db_session):
        with pytest.raises(NotFoundError):
            BackgroundJobService(db_session).cancel_job(999)

    def test_remark_can_be_set_and_cleared(self, db_session, source_dir, background_image):
        job = self._job(db_session, source_dir, background_image)
        service = BackgroundJobService(db_session)
        assert service.update_remark(job.id, JobRemarkUpdate(remark="  第一批  ")).remark == "第一批"
        assert service.update_remark(job.id, JobRemarkUpdate(remark="")).remark == ""

    def test_delete_requires_terminal_state(self, db_session, source_dir, background_image):
        job = self._job(db_session, source_dir, background_image)
        with pytest.raises(ConflictError, match="尚未结束"):
            BackgroundJobService(db_session).delete_job(job.id)

    def test_delete_purges_the_task_directory(self, db_session, source_dir, background_image):
        """purge_files=True 清掉任务独占的产物目录（整棵，不留空壳）。"""
        job = self._job(db_session, source_dir, background_image)
        service = BackgroundJobService(db_session)
        service.cancel_job(job.id)
        out_dir = Path(job.output_dir)
        (out_dir / "羊1.png").write_bytes(b"fake")

        service.delete_job(job.id, purge_files=True)
        assert not out_dir.exists()

    def test_delete_without_purge_keeps_files(self, db_session, source_dir, background_image):
        job = self._job(db_session, source_dir, background_image)
        service = BackgroundJobService(db_session)
        service.cancel_job(job.id)
        out_dir = Path(job.output_dir)
        (out_dir / "羊1.png").write_bytes(b"fake")

        service.delete_job(job.id)
        assert (out_dir / "羊1.png").exists()

    def test_batch_delete_is_all_or_nothing(self, db_session, source_dir, background_image):
        """批量删除里有一条不可删 → 整批回滚，并指出卡在哪条。"""
        job = self._job(db_session, source_dir, background_image)
        service = BackgroundJobService(db_session)
        with pytest.raises(ConflictError, match=f"#{job.id}"):
            service.delete_jobs([job.id])
        assert service.get_job(job.id) is not None

    def test_batch_delete_happy_path(self, db_session, source_dir, background_image):
        service = BackgroundJobService(db_session)
        ids = [
            self._job(db_session, source_dir, background_image).id for _ in range(3)
        ]
        for job_id in ids:
            service.cancel_job(job_id)
        assert service.delete_jobs(ids) == ids
        with pytest.raises(NotFoundError):
            service.get_job(ids[0])


class TestListing:
    """列表与详情。"""

    def test_list_is_newest_first_and_paginates(
        self, db_session, source_dir, background_image
    ):
        service = BackgroundJobService(db_session)
        for _ in range(3):
            service.create_job(_payload(source_dir, background_image))

        page, total = service.list_jobs(page=1, page_size=2)
        assert total == 3
        assert [j.id for j in page] == [3, 2]

        rest, _ = service.list_jobs(page=2, page_size=2)
        assert [j.id for j in rest] == [1]

    def test_list_filters_by_status(self, db_session, source_dir, background_image):
        service = BackgroundJobService(db_session)
        first = service.create_job(_payload(source_dir, background_image))
        service.create_job(_payload(source_dir, background_image))
        service.cancel_job(first.id)

        page, total = service.list_jobs(status=BackgroundJobStatus.CANCELLED)
        assert total == 1
        assert [j.id for j in page] == [first.id]

    def test_get_unknown_job(self, db_session):
        with pytest.raises(NotFoundError):
            BackgroundJobService(db_session).get_job(999)


class TestOutputPath:
    """产物定位：只由任务记录推导，越界与未产出都回 404。"""

    def _job(self, db_session, source_dir, background_image):
        return BackgroundJobService(db_session).create_job(
            _payload(source_dir, background_image)
        )

    def test_index_is_bounds_checked(self, db_session, source_dir, background_image):
        job = self._job(db_session, source_dir, background_image)
        service = BackgroundJobService(db_session)
        with pytest.raises(NotFoundError, match="产物不存在"):
            service.get_output_path(job.id, 99)
        with pytest.raises(NotFoundError, match="产物不存在"):
            service.get_output_path(job.id, 0)

    def test_unproduced_file_is_404(self, db_session, source_dir, background_image):
        """条目在、文件还没产出（还没轮到 or 失败了）→ 404，前端只关心能不能显示。"""
        job = self._job(db_session, source_dir, background_image)
        with pytest.raises(NotFoundError, match="还没有生成"):
            BackgroundJobService(db_session).get_output_path(job.id, 1)

    def test_returns_the_registered_artifact(self, db_session, source_dir, background_image):
        job = self._job(db_session, source_dir, background_image)
        item = job.items[0]
        Path(item.output_path).write_bytes(b"fake-png")

        path, name, source_name = BackgroundJobService(db_session).get_output_path(job.id, 1)
        assert path == Path(item.output_path)
        assert name == "羊1.png"
        assert source_name == "羊1.png"

    def test_path_only_comes_from_the_job_record(
        self, db_session, source_dir, background_image
    ):
        """产物必须在任务目录里 —— 路径由任务记录推导，前端传不进任何路径片段。"""
        job = self._job(db_session, source_dir, background_image)
        item = job.items[0]
        Path(item.output_path).write_bytes(b"fake-png")

        path, _, _ = BackgroundJobService(db_session).get_output_path(job.id, 1)
        assert path.parent == Path(job.output_dir)
