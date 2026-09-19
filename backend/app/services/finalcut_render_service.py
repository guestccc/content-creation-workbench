"""一键成品·合成任务的业务逻辑层。

职责边界与 mix_job_service.py 一致：本模块只管「状态机 + 查询 + 创建校验」，
不碰子进程（那是 finalcut_render_runner.py 的事）。

创建时的校验（都返回可读的中文错误，不做「排队后才炸」）：
- 视频必须存在且能被 ffprobe 探出规格（规格快照落库，烧字时换算框选坐标）；
- 必须探测到中文字体（没有字体烧出来是方块，这是环境类问题，创建时就拦住）；
- copy_job_id 若给必须是存在的文案任务（仅溯源用，删除文案任务不影响成片记录）。

产物结构：`output_dir/finalcut-<时间戳>/01.mp4`，删除任务时 purge_files=True
整棵删（与混剪同一套「删任务要问是否连产物一起删」的规矩）。
"""

import random
import time
from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError, ConflictError, DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.core.materials import FINALCUT, subdir
from app.db.session import transaction
from app.models.content import utcnow
from app.models.finalcut_job import (
    FinalcutCopyJob,
    FinalcutItemStatus,
    FinalcutRenderItem,
    FinalcutRenderJob,
    FinalcutRenderJobStatus,
)
from app.models.mix_job import MixJob, MixJobItem, MixOutputStatus
from app.models.subtitle_job import SubtitleJobItem, SubtitleJobItemStatus
from app.schemas.finalcut_job import FinalcutRenderJobCreate
from app.services.finalcut_env import detect_font
from app.services.fs_cleanup import remove_paths_best_effort
from app.services.mix_runner import probe_video_spec

logger = get_logger(__name__)

#: 历史产物来源清单的条数上限（页面只需要最近的素材，全量没有意义）。
_SOURCES_LIMIT = 50


def _product_paths(job: FinalcutRenderJob) -> List[Path]:
    """列出一条任务的产物路径（只从任务记录推导，不接受外部路径）。

    整个输出目录（finalcut-<时间戳>，创建时 exist_ok=False 保证任务独占）
    整棵删 —— 比逐条成片删更干净，失败的半成品也不会留下空目录。
    """
    return [Path(job.output_dir)] if job.output_dir else []


def _purge_products(products: List[Path], *, job_ids: List[int]) -> None:
    """best-effort 清产物：有失败的记一条汇总日志，不向上抛。"""
    if not products:
        return
    failed = remove_paths_best_effort(products)
    if failed:
        logger.warning(
            "任务产物清理有残留 | jobs=%s | 失败=%s 个路径", job_ids, len(failed)
        )


class FinalcutRenderJobService:
    """一键成品·合成任务服务。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    # ------------------------------------------------------------------
    # 历史产物来源（选素材步骤用）
    # ------------------------------------------------------------------

    def list_sources(self) -> dict:
        """列出可用的历史产物：混剪的成片 + 字幕任务的 .srt。

        只收「任务成功且文件还在磁盘上」的条目，按任务倒序、条数封顶。
        返回绝对路径 —— 前端拿去创建任务，创建时再走一遍完整校验。
        """
        videos: List[dict] = []
        rows = self.db.execute(
            select(MixJobItem, MixJob.id)
            .join(MixJob, MixJobItem.job_id == MixJob.id)
            .where(MixJobItem.status == MixOutputStatus.SUCCESS)
            .order_by(MixJob.id.desc(), MixJobItem.index.asc())
            .limit(_SOURCES_LIMIT * 2)  # 文件可能已被删，多取一些再过滤
        ).all()
        for item, job_id in rows:
            if len(videos) >= _SOURCES_LIMIT:
                break
            path = Path(item.output_path or "")
            if not item.output_path or not path.is_file():
                continue
            videos.append(
                {
                    "path": str(path),
                    "name": f"混剪 #{job_id} / {item.output_name}",
                    "origin": "mix",
                    "job_id": job_id,
                    "duration_seconds": item.duration_seconds,
                    "size_bytes": item.size_bytes,
                    "video_url": f"/api/v1/mix/jobs/{job_id}/outputs/{item.index}/video",
                    "thumb_url": f"/api/v1/mix/jobs/{job_id}/outputs/{item.index}/thumb",
                }
            )

        subtitles: List[dict] = []
        rows = self.db.execute(
            select(SubtitleJobItem)
            .where(
                SubtitleJobItem.status == SubtitleJobItemStatus.SUCCESS,
                SubtitleJobItem.subtitle_exists.is_(True),
            )
            .order_by(SubtitleJobItem.id.desc())
            .limit(_SOURCES_LIMIT * 2)
        ).scalars().all()
        for item in rows:
            if len(subtitles) >= _SOURCES_LIMIT:
                break
            path = Path(item.output_path or "")
            if not item.output_path or not path.is_file():
                continue
            subtitles.append(
                {
                    "path": str(path),
                    "name": f"字幕 #{item.job_id} / {path.name}",
                    "origin": "subtitle",
                    "job_id": item.job_id,
                    "duration_seconds": item.duration_seconds,
                    "size_bytes": item.file_size,
                    "video_url": "",
                    "thumb_url": "",
                }
            )

        return {"subtitles": subtitles, "videos": videos}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _get_job_or_404(self, job_id: int) -> FinalcutRenderJob:
        """按 ID 取任务，不存在则抛 NotFoundError。"""
        job = self.db.get(FinalcutRenderJob, job_id)
        if job is None:
            raise NotFoundError(f"合成任务不存在：id={job_id}")
        return job

    def get_job(self, job_id: int) -> FinalcutRenderJob:
        """按 ID 获取任务（含每条成片的明细），前端轮询进度也用它。"""
        try:
            return self._get_job_or_404(job_id)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("查询合成任务失败 | id=%s", job_id)
            raise DatabaseError("查询合成任务失败") from exc

    def list_jobs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None,
    ) -> Tuple[List[FinalcutRenderJob], int]:
        """分页查询任务，按创建时间倒序。"""
        try:
            conditions = []
            if status:
                conditions.append(FinalcutRenderJob.status == status)

            count_stmt = select(func.count()).select_from(FinalcutRenderJob)
            list_stmt = select(FinalcutRenderJob)
            for condition in conditions:
                count_stmt = count_stmt.where(condition)
                list_stmt = list_stmt.where(condition)

            total = self.db.execute(count_stmt).scalar_one()
            list_stmt = (
                list_stmt.order_by(FinalcutRenderJob.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            items = list(self.db.execute(list_stmt).scalars().all())
            return items, int(total)
        except SQLAlchemyError as exc:
            logger.exception("查询合成任务列表失败")
            raise DatabaseError("查询合成任务列表失败") from exc

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def create_job(self, payload: FinalcutRenderJobCreate) -> FinalcutRenderJob:
        """创建合成任务：校验素材与环境、探测规格与字体、预建输出目录、落库排队。

        Raises:
            BadRequestError: 视频不存在/探不出规格/没有中文字体/来源任务不存在/输出目录不合法。
            DatabaseError: 写库失败。
        """
        video = Path(payload.video_path)
        if not video.is_file():
            raise BadRequestError(f"视频文件不存在：{payload.video_path}")

        spec = probe_video_spec(video)
        if spec is None or not spec.get("duration"):
            raise BadRequestError(
                f"读不出视频规格（ffprobe 不可用或文件损坏）：{video.name}"
            )

        # 字体在创建时探测并固化进任务记录：排队期间用户换了字体配置
        # 也不影响已排队的任务（与素材路径同一套「创建时固化」语义）
        font = detect_font()
        if font is None:
            raise BadRequestError(
                "没有探测到中文字体，烧进画面的文案会是方块："
                "请安装中文字体，或在 backend/.env 里用 FINALCUT_FONT_FILE 指定"
            )

        if payload.copy_job_id is not None:
            copy_job = self.db.get(FinalcutCopyJob, payload.copy_job_id)
            if copy_job is None:
                raise BadRequestError(f"来源文案任务不存在：id={payload.copy_job_id}")

        if payload.output_dir:
            output_root = Path(payload.output_dir)
            if not output_root.is_dir():
                raise BadRequestError(f"输出目录不存在或不是目录：{payload.output_dir}")
        else:
            output_root = subdir(FINALCUT)
            output_root.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output_dir = output_root / f"finalcut-{timestamp}"
        try:
            output_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            # 同一秒撞名：补一个随机后缀
            output_dir = output_root / f"finalcut-{timestamp}-{random.randint(100, 999)}"
            output_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise BadRequestError(f"创建输出目录失败：{output_dir}（{exc}）") from exc

        try:
            with transaction(self.db):
                job = FinalcutRenderJob(
                    status=FinalcutRenderJobStatus.PENDING,
                    copy_job_id=payload.copy_job_id,
                    video_path=str(video),
                    video_duration=float(spec["duration"]),
                    video_spec={
                        "width": spec["width"],
                        "height": spec["height"],
                        "fps_num": spec["fps_num"],
                        "fps_den": spec["fps_den"],
                        "has_audio": spec["has_audio"],
                        "audio_codec": spec.get("audio_codec", ""),
                    },
                    output_dir=str(output_dir),
                    font_file=font.file,
                    total_items=len(payload.items),
                )
                self.db.add(job)
                self.db.flush()

                for index, item_spec in enumerate(payload.items, start=1):
                    self.db.add(
                        FinalcutRenderItem(
                            job_id=job.id,
                            index=index,
                            copy_text=item_spec.copy_text,
                            angle=item_spec.angle,
                            style=item_spec.style,
                            box=item_spec.box.model_dump(),
                            font_size=item_spec.font_size,
                        )
                    )
                self.db.flush()

            logger.info(
                "合成任务创建成功 | id=%s | 成片数=%s | 输出=%s",
                job.id, job.total_items, output_dir,
            )
            return job
        except SQLAlchemyError as exc:
            logger.exception("合成任务创建失败")
            raise DatabaseError("合成任务创建失败") from exc

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------

    def cancel_job(self, job_id: int) -> FinalcutRenderJob:
        """取消任务（与混剪同一套做法：置状态列，runner 整组杀当前 ffmpeg）。"""
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)
                if job.status in FinalcutRenderJobStatus.TERMINAL:
                    raise ConflictError(f"任务已是终态（{job.status}），无法取消")
                job.status = FinalcutRenderJobStatus.CANCELLED
                job.finished_at = utcnow()
                self.db.flush()
            logger.info("合成任务已取消 | id=%s", job_id)
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("取消合成任务失败 | id=%s", job_id)
            raise DatabaseError("取消合成任务失败") from exc

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------

    def delete_job(self, job_id: int, *, purge_files: bool = False) -> None:
        """删除任务记录（级联删除成片条目）。

        仅允许删除终态任务。默认只删记录；purge_files=True 时把任务的输出
        目录整棵删掉。产物清理在记录提交之后 best-effort 执行，个别文件
        被占用不阻断删除。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务尚未结束。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)
                if job.status not in FinalcutRenderJobStatus.TERMINAL:
                    raise ConflictError(f"任务尚未结束（{job.status}），请先取消后再删除")
                products = _product_paths(job) if purge_files else []
                self.db.delete(job)
            logger.info("合成任务已删除 | id=%s | 清产物=%s", job_id, purge_files)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("删除合成任务失败 | id=%s", job_id)
            raise DatabaseError("删除合成任务失败") from exc

        _purge_products(products, job_ids=[job_id])

    def delete_jobs(self, job_ids: List[int], *, purge_files: bool = False) -> List[int]:
        """批量删除任务记录：全部成功才提交，任何一个不可删则整批回滚。

        Raises:
            NotFoundError: 某个任务不存在（整批回滚）。
            ConflictError: 某个任务尚未结束（整批回滚）。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                products: List[Path] = []
                for job_id in job_ids:
                    job = self._get_job_or_404(job_id)
                    if job.status not in FinalcutRenderJobStatus.TERMINAL:
                        raise ConflictError(
                            f"任务 #{job_id} 尚未结束（{job.status}），请先取消后再删除"
                        )
                    if purge_files:
                        products.extend(_product_paths(job))
                    self.db.delete(job)
            logger.info("合成任务批量删除 | ids=%s | 清产物=%s", job_ids, purge_files)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("批量删除合成任务失败 | ids=%s", job_ids)
            raise DatabaseError("批量删除任务失败") from exc

        _purge_products(products, job_ids=job_ids)
        return job_ids

    # ------------------------------------------------------------------
    # 成片定位（供播放/封面接口用；路径完全由任务记录推导）
    # ------------------------------------------------------------------

    def get_output_path(self, job_id: int, output_index: int) -> Tuple[Path, str]:
        """按序号定位一条成片文件。

        Raises:
            NotFoundError: 任务不存在 / 序号越界 / 该条未成功 / 文件已被移走。
        """
        job = self.get_job(job_id)
        item = next((it for it in job.items if it.index == output_index), None)
        if item is None:
            raise NotFoundError(f"成片不存在：job={job_id}, index={output_index}")
        if item.status != FinalcutItemStatus.SUCCESS or not item.output_path:
            raise NotFoundError(f"该条成片尚未产出（状态：{item.status}）")
        path = Path(item.output_path)
        if not path.is_file():
            raise NotFoundError(f"成片文件已被移动或删除：{item.output_name}")
        return path, item.output_name
