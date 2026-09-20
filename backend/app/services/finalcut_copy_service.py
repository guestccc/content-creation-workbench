"""一键成品·文案生成任务的业务逻辑层。

职责边界与 mix_job_service.py 一致：本模块只管「状态机 + 查询 + 创建校验」，
不碰 AI 调用（那是 finalcut_copy_runner.py 的事）。

与其它任务域的两点不同：
- **磁盘上没有任何产物**：产出全部在 `result` 列里（JSON）。所以删除没有
  purge_files 参数，删除确认文案也要说清楚「只删记录」；
- **取消不是即时的**：runner 只在相位边界（读完字幕 / HTTP 返回 / 解析完）
  重读状态列弃结果，在途 HTTP 无法中断，延迟上界 = AI_TIMEOUT_SECONDS。
  取消接口本身仍然只是把状态置为 cancelled，语义不变。

创建校验（都在创建时报清楚，不做「排队后才炸」）：
- 字幕与视频必须是已存在的文件（路径与后缀在 schema 层已校验）；
- 视频必须能被 ffprobe 探出时长（时长是 AI 定量的依据，探不出来没法干）；
- 字幕拆解后必须有可用文本（空字幕排队也是白排）。
"""

from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import BadRequestError, ConflictError, DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.db.session import transaction
from app.models.content import utcnow
from app.models.finalcut_job import FinalcutCopyJob, FinalcutCopyJobStatus
from app.schemas.common import JobRemarkUpdate
from app.schemas.finalcut_job import FinalcutCopyJobCreate
from app.services.finalcut_copy import srt_to_material
from app.services.media_tools import decode_output
from app.services.mix_runner import probe_video_spec

logger = get_logger(__name__)


def read_subtitle_material(path: Path) -> str:
    """读字幕文件并拆解成素材文本（创建校验与 runner 共用同一入口）。

    编码容忍与 media_tools.decode_output 同一套：先 UTF-8 再本地代码页，
    坏字节替换 —— 字幕文件可能来自任意来源，读不出来不该是 UnicodeDecodeError。
    """
    text = decode_output(path.read_bytes())
    material, _truncated = srt_to_material(text)
    return material


class FinalcutCopyJobService:
    """文案生成任务服务。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _get_job_or_404(self, job_id: int) -> FinalcutCopyJob:
        """按 ID 取任务，不存在则抛 NotFoundError。"""
        job = self.db.get(FinalcutCopyJob, job_id)
        if job is None:
            raise NotFoundError(f"文案任务不存在：id={job_id}")
        return job

    def get_job(self, job_id: int) -> FinalcutCopyJob:
        """按 ID 获取任务，前端轮询进度也用它。"""
        try:
            return self._get_job_or_404(job_id)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("查询文案任务失败 | id=%s", job_id)
            raise DatabaseError("查询文案任务失败") from exc

    def list_jobs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None,
    ) -> Tuple[List[FinalcutCopyJob], int]:
        """分页查询任务，按创建时间倒序。"""
        try:
            conditions = []
            if status:
                conditions.append(FinalcutCopyJob.status == status)

            count_stmt = select(func.count()).select_from(FinalcutCopyJob)
            list_stmt = select(FinalcutCopyJob)
            for condition in conditions:
                count_stmt = count_stmt.where(condition)
                list_stmt = list_stmt.where(condition)

            total = self.db.execute(count_stmt).scalar_one()
            list_stmt = (
                list_stmt.order_by(FinalcutCopyJob.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            items = list(self.db.execute(list_stmt).scalars().all())
            return items, int(total)
        except SQLAlchemyError as exc:
            logger.exception("查询文案任务列表失败")
            raise DatabaseError("查询文案任务列表失败") from exc

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def create_job(self, payload: FinalcutCopyJobCreate) -> FinalcutCopyJob:
        """创建文案生成任务：校验素材、探测时长、落库排队。

        Raises:
            BadRequestError: 文件不存在 / 视频探不出规格 / 字幕没有可用文本。
            DatabaseError: 写库失败。
        """
        subtitle = Path(payload.subtitle_path)
        if not subtitle.is_file():
            raise BadRequestError(f"字幕文件不存在：{payload.subtitle_path}")
        video = Path(payload.video_path)
        if not video.is_file():
            raise BadRequestError(f"视频文件不存在：{payload.video_path}")

        spec = probe_video_spec(video)
        if spec is None or not spec.get("duration"):
            raise BadRequestError(
                f"读不出视频规格（ffprobe 不可用或文件损坏）：{video.name}"
            )

        try:
            material = read_subtitle_material(subtitle)
        except OSError as exc:
            raise BadRequestError(f"字幕文件读不出来：{subtitle.name}（{exc}）") from exc
        if not material.strip():
            raise BadRequestError(f"字幕文件里没有可用文本：{subtitle.name}")

        try:
            with transaction(self.db):
                job = FinalcutCopyJob(
                    status=FinalcutCopyJobStatus.PENDING,
                    subtitle_path=str(subtitle),
                    video_path=str(video),
                    video_duration=float(spec["duration"]),
                    copy_count=payload.copy_count,
                    hint=payload.hint,
                    model=settings.AI_MODEL,
                )
                self.db.add(job)
                self.db.flush()
            logger.info(
                "文案任务创建成功 | id=%s | 时长=%.1fs | 条数=%s",
                job.id, job.video_duration, job.copy_count,
            )
            return job
        except SQLAlchemyError as exc:
            logger.exception("文案任务创建失败")
            raise DatabaseError("文案任务创建失败") from exc

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------

    def cancel_job(self, job_id: int) -> FinalcutCopyJob:
        """取消任务。

        语义与其它任务域一致（置状态列），但生效时机不同：runner 只在相位
        边界弃结果，在途的 AI 请求会跑完（上界 AI_TIMEOUT_SECONDS），结果丢弃。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)
                if job.status in FinalcutCopyJobStatus.TERMINAL:
                    raise ConflictError(f"任务已是终态（{job.status}），无法取消")
                job.status = FinalcutCopyJobStatus.CANCELLED
                job.finished_at = utcnow()
                self.db.flush()
            logger.info("文案任务已取消 | id=%s", job_id)
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("取消文案任务失败 | id=%s", job_id)
            raise DatabaseError("取消文案任务失败") from exc

    # ------------------------------------------------------------------
    # 备注
    # ------------------------------------------------------------------

    def update_remark(self, job_id: int, payload: JobRemarkUpdate) -> FinalcutCopyJob:
        """更新任务备注（空串表示清空）。

        备注纯属用户标记，不参与状态机：终态任务也能改（回头补个标记很正常）。

        Raises:
            NotFoundError: 任务不存在。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)
                job.remark = payload.remark
                self.db.flush()

            logger.info("文案任务备注已更新 | id=%s", job_id)
            return job
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("更新文案任务备注失败 | id=%s", job_id)
            raise DatabaseError("更新任务备注失败") from exc

    # ------------------------------------------------------------------
    # 删除（磁盘上无产物，所以没有 purge_files 参数）
    # ------------------------------------------------------------------

    def delete_job(self, job_id: int) -> None:
        """删除任务记录。文案任务没有磁盘产物，删的就是记录本身。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务尚未结束。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)
                if job.status not in FinalcutCopyJobStatus.TERMINAL:
                    raise ConflictError(f"任务尚未结束（{job.status}），请先取消后再删除")
                self.db.delete(job)
            logger.info("文案任务已删除 | id=%s", job_id)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("删除文案任务失败 | id=%s", job_id)
            raise DatabaseError("删除文案任务失败") from exc

    def delete_jobs(self, job_ids: List[int]) -> List[int]:
        """批量删除任务记录：全部成功才提交，任何一个不可删则整批回滚。

        Raises:
            NotFoundError: 某个任务不存在（整批回滚）。
            ConflictError: 某个任务尚未结束（整批回滚）。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                for job_id in job_ids:
                    job = self._get_job_or_404(job_id)
                    if job.status not in FinalcutCopyJobStatus.TERMINAL:
                        raise ConflictError(
                            f"任务 #{job_id} 尚未结束（{job.status}），请先取消后再删除"
                        )
                    self.db.delete(job)
            logger.info("文案任务批量删除 | ids=%s", job_ids)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("批量删除文案任务失败 | ids=%s", job_ids)
            raise DatabaseError("批量删除任务失败") from exc
        return job_ids
