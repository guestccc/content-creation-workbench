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


def read_subtitle_material(path: Path) -> Tuple[str, bool]:
    """读字幕文件并拆解成素材文本（创建校验、runner、预览接口共用同一入口）。

    编码容忍与 media_tools.decode_output 同一套：先 UTF-8 再本地代码页，
    坏字节替换 —— 字幕文件可能来自任意来源，读不出来不该是 UnicodeDecodeError。

    Returns:
        (素材文本, 是否因超过 FINALCUT_SRT_MAX_CHARS 被截断)。
    """
    text = decode_output(path.read_bytes())
    return srt_to_material(text)


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

    def read_subtitle_preview(self, job_id: int) -> dict:
        """读一条文案任务用的字幕：原文 + 喂给 AI 的素材（第 ② 步左右对照用）。

        路径**完全由任务记录推导**，不接受任何前端传入的路径片段 —— 同
        subtitle_job_service.get_subtitle_path 的安全模型。

        素材是**现读现算**的，不是任务执行时的快照：输入可复现（不像
        raw_response 那份不可复现的 AI 输出，必须留），现算让排队中/进行中/
        失败/取消/历史任务都能看到这份素材，代价只是「文件在任务后被改写的话
        会与当时不一致」。素材走 read_subtitle_material —— 与 runner 读的是
        同一个函数，所以「AI 输入的素材」名副其实。

        Raises:
            NotFoundError: 任务不存在、字幕文件已不在磁盘上或读不出来。
            DatabaseError: 查库失败。
        """
        job = self.get_job(job_id)
        path = Path(job.subtitle_path)
        if not path.is_file():
            raise NotFoundError(f"字幕文件已不在磁盘上：{path.name}")

        # 素材：与 runner 同一入口（本函数会自己再读一次盘，字幕文件只有几 KB，
        # 多读一次的代价远小于「两个视图的素材算法各写一份」的维护成本）
        try:
            material, material_truncated = read_subtitle_material(path)
        except OSError as exc:
            raise NotFoundError(f"字幕文件读不出来：{path.name}（{exc}）") from exc

        # 原文：按字节截断后再解码，避免一次读入超大文件（镜像 subtitle_jobs
        # 的预览接口）。解码走 decode_output 而不是那里的 utf-8-sig —— 本地
        # .srt 可能是 GBK，两个视图必须用同一套解码，否则会出现「原文乱码、
        # 素材正常」。.ass/.vtt 没有空行分块，rfind 返回 -1，退化成硬截断。
        max_bytes = settings.SUBTITLE_PREVIEW_MAX_BYTES
        try:
            size_bytes = path.stat().st_size
            truncated = size_bytes > max_bytes
            with path.open("rb") as handle:
                raw = handle.read(max_bytes + 1 if truncated else max_bytes)
        except OSError as exc:
            raise NotFoundError(f"字幕文件读不出来：{path.name}（{exc}）") from exc

        content = decode_output(raw[:max_bytes])
        if truncated:
            # 砍掉最后一个可能截断到一半的字幕块，让预览结尾是完整的
            last_boundary = content.rfind("\n\n")
            if last_boundary > 0:
                content = content[:last_boundary]

        return {
            "path": str(path),
            "name": path.name,
            "size_bytes": size_bytes,
            "content": content,
            "truncated": truncated,
            "material": material,
            "material_truncated": material_truncated,
        }

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
        """创建文案生成任务：校验素材、探测时长、快照语速、落库排队。

        语速（字数预算 = 时长 × 语速）按任务存：请求里带 `chars_per_second`
        就用它，不带就用当前全局默认值 —— 快照之后不再回头看全局配置。

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
            material, _truncated = read_subtitle_material(subtitle)
        except OSError as exc:
            raise BadRequestError(f"字幕文件读不出来：{subtitle.name}（{exc}）") from exc
        if not material.strip():
            raise BadRequestError(f"字幕文件里没有可用文本：{subtitle.name}")

        # 语速在创建时**快照**进任务记录：请求带了就用它（这条念快/念慢），
        # 没带就用当前全局默认值。之后改全局配置不影响已建的任务 ——
        # 历史任务的预算与页面上算出的秒数必须与当时生成的内容对得上。
        rate = (
            payload.chars_per_second
            if payload.chars_per_second is not None
            else float(settings.FINALCUT_CHARS_PER_SECOND)
        )

        try:
            with transaction(self.db):
                job = FinalcutCopyJob(
                    status=FinalcutCopyJobStatus.PENDING,
                    subtitle_path=str(subtitle),
                    video_path=str(video),
                    video_duration=float(spec["duration"]),
                    chars_per_second=rate,
                    copy_count=payload.copy_count,
                    hint=payload.hint,
                    model=settings.AI_MODEL,
                )
                self.db.add(job)
                self.db.flush()
            logger.info(
                "文案任务创建成功 | id=%s | 时长=%.1fs | 语速=%s 字/秒 | 条数=%s",
                job.id, job.video_duration, job.chars_per_second, job.copy_count,
            )
            return job
        except SQLAlchemyError as exc:
            logger.exception("文案任务创建失败")
            raise DatabaseError("文案任务创建失败") from exc

    # ------------------------------------------------------------------
    # 重试
    # ------------------------------------------------------------------

    def retry_job(self, job_id: int) -> FinalcutCopyJob:
        """整任务重跑：拿旧任务的参数**新建一条任务**并返回它。

        这条任务没有条目级状态（一次 AI 调用产出一批文案），所以「重试」只有
        整任务重跑一种形态。做成新建而不是就地重跑的理由：一条任务的产物就是
        「那一批文案」，就地重跑会把上一批覆盖掉，历史记录与产物就再也对不上；
        新建之后旧任务原样留档，用户能在历史里对比两批文案的差别。

        入参全部能从旧任务行拿到（subtitle_path / video_path / chars_per_second
        / copy_count / hint），语速也照抄**当时快照的值**而不是今天的全局默认。

        Raises:
            NotFoundError: 任务不存在。
            BadRequestError: 素材已删或读不出来 —— 由 create_job 抛出，点击即报错。
            DatabaseError: 写库失败（事务已回滚）。
        """
        source = self.get_job(job_id)
        payload = FinalcutCopyJobCreate(
            subtitle_path=source.subtitle_path,
            video_path=source.video_path,
            chars_per_second=source.chars_per_second,
            copy_count=source.copy_count,
            hint=source.hint,
        )
        job = self.create_job(payload)
        logger.info("文案任务重试（新建） | 原任务=%s | 新任务=%s", job_id, job.id)
        return job

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
