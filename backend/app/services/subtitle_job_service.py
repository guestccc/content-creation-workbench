"""字幕提取任务的业务逻辑层。

职责边界（与 scene_job_service.py 一致）：
- 本模块只管「状态机 + 查询 + 创建时的文件枚举」，完全不碰子进程；
- 真正起 VideoCaptioner 子进程的执行逻辑在 subtitle_runner.py，
  工作线程在 subtitle_job_worker.py。

任务生命周期：

    pending ──工作线程认领──> running ──全部成功──> success
       │                       │
       │                       ├──部分失败──> partial
       │                       └──全部失败──> failed
       │
       └──取消──> cancelled（running 中取消：未开始的条目标记 skipped）

终态（success / partial / failed / cancelled）只能通过删除记录清理。
不提供自动重试：视频没声音、文件损坏都是确定性错误，自动重跑只会再烧一遍时间。
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core import media_files
from app.core.config import settings
from app.core.exceptions import BadRequestError, ConflictError, DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.core.materials import SUBTITLE, subdir
from app.core.output_paths import allocate_unique_paths
from app.core.subtitle_asr import resolve_engine
from app.db.session import transaction
from app.models.content import utcnow
from app.services.fs_cleanup import remove_paths_best_effort
from app.models.subtitle_job import (
    SubtitleJob,
    SubtitleJobItem,
    SubtitleJobItemStatus,
    SubtitleJobStatus,
)
from app.schemas.common import JobRemarkUpdate
from app.schemas.subtitle_job import OUTPUT_FORMAT, SubtitleJobCreate
from app.services.subtitle_env import detect

logger = get_logger(__name__)


def is_video_file(name: str) -> bool:
    """按后端配置的扩展名白名单判断是否为可处理的视频文件。"""
    return media_files.is_media_file(name, settings.SUBTITLE_INPUT_EXTENSIONS)


def enumerate_videos(
    input_path: str, *, recursive: bool, files: Optional[List[str]] = None
) -> List[Path]:
    """把输入路径展开成一份确定的视频清单（白名单与批量上限取本功能的配置）。

    规则与异常见 core/media_files.enumerate_videos —— 那里是唯一实现。
    """
    return media_files.enumerate_videos(
        input_path,
        recursive=recursive,
        files=files,
        extensions=settings.SUBTITLE_INPUT_EXTENSIONS,
        max_files=settings.SUBTITLE_MAX_BATCH_FILES,
    )


def allocate_output_paths(
    videos: List[Path], output_dir: Path, *, format_suffix: str = f".{OUTPUT_FORMAT}"
) -> Dict[Path, Path]:
    """为每条视频分配一个字幕输出文件，重名的加 -2 / -3 后缀。

    与镜头分割不同，这里**允许**目标目录里已经有同名文件：字幕是「一条视频
    一份」的产物，用户很可能分批提同一条视频（换识别语言再跑一次）。所以不报错，
    改成换个名字，谁都不覆盖谁。

    预建输出目录（而不是等到子进程里）：`-o` 传的是带扩展名的完整文件路径，
    VideoCaptioner 的文件模式分支不会自己创建父目录 —— 目录必须在这里就位。

    编号规则本身在 core/output_paths.py（换背景要用同一套），这里只填命名规则。

    Raises:
        BadRequestError: 输出目录建不出来（权限、只读盘）。
    """
    return allocate_unique_paths(
        videos,
        output_dir,
        lambda video, count: (
            f"{video.stem}{format_suffix}"
            if count == 1
            else f"{video.stem}-{count}{format_suffix}"
        ),
    )


def _product_paths(job: SubtitleJob) -> List[Path]:
    """列出一条任务的产物路径（只从任务记录推导，不接受外部路径）。

    各条目的字幕文件（.srt）。文件没产出的条目（失败/跳过）路径仍在记录里，
    不存在时清理函数会静默跳过。
    """
    return [Path(item.output_path) for item in job.items if item.output_path]


def _purge_products(products: List[Path], *, job_ids: List[int]) -> None:
    """best-effort 清产物：有失败的记一条汇总日志，不向上抛。"""
    if not products:
        return
    failed = remove_paths_best_effort(products)
    if failed:
        logger.warning(
            "任务产物清理有残留 | jobs=%s | 失败=%s 个路径", job_ids, len(failed)
        )


class SubtitleJobService:
    """字幕提取任务服务。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _get_job_or_404(self, job_id: int) -> SubtitleJob:
        """按 ID 取任务，不存在则抛 NotFoundError。"""
        job = self.db.get(SubtitleJob, job_id)
        if job is None:
            raise NotFoundError(f"字幕提取任务不存在：id={job_id}")
        return job

    def get_job(self, job_id: int) -> SubtitleJob:
        """按 ID 获取任务（含每条视频的明细）。"""
        try:
            return self._get_job_or_404(job_id)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("查询字幕提取任务失败 | id=%s", job_id)
            raise DatabaseError("查询字幕提取任务失败") from exc

    def list_jobs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None,
    ) -> Tuple[List[SubtitleJob], int]:
        """分页查询任务，按创建时间倒序（即 ID 倒序）。

        Returns:
            (当前页数据, 满足条件的总条数)
        """
        try:
            conditions = []
            if status:
                conditions.append(SubtitleJob.status == status)

            count_stmt = select(func.count()).select_from(SubtitleJob)
            list_stmt = select(SubtitleJob)
            for condition in conditions:
                count_stmt = count_stmt.where(condition)
                list_stmt = list_stmt.where(condition)

            total = self.db.execute(count_stmt).scalar_one()

            list_stmt = (
                list_stmt.order_by(SubtitleJob.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            items = list(self.db.execute(list_stmt).scalars().all())
            return items, int(total)
        except SQLAlchemyError as exc:
            logger.exception("查询字幕提取任务列表失败")
            raise DatabaseError("查询字幕提取任务列表失败") from exc

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def create_job(self, payload: SubtitleJobCreate) -> SubtitleJob:
        """创建字幕提取任务：枚举视频、分配输出文件、落库排队。

        创建成功即返回，真正的转写由后台工作线程认领执行。

        Raises:
            BadRequestError: 未装 VideoCaptioner、输入路径不合法、目录里没有视频、
                输出目录建不出来。
            DatabaseError: 写库失败（事务已回滚）。
        """
        # 建任务前先确认工具可用：探测结果有缓存，这里只是读缓存不贵。
        # 失败要快 —— 让用户在点击时就看到原因，而不是排队几分钟后整条失败。
        if not detect().installed:
            raise BadRequestError(
                "未检测到可用的 VideoCaptioner，请先按页面上方的指引安装后再试"
            )

        videos = enumerate_videos(
            payload.input_path, recursive=payload.recursive, files=payload.files
        )
        output_dir = Path(payload.output_dir) if payload.output_dir else subdir(SUBTITLE)

        params = {
            "asr": resolve_engine(payload.asr),
            "language": payload.language or "",
            "format": OUTPUT_FORMAT,
        }

        try:
            with transaction(self.db):
                job = SubtitleJob(
                    status=SubtitleJobStatus.PENDING,
                    input_path=payload.input_path,
                    output_dir=str(output_dir),
                    recursive=payload.recursive,
                    params=params,
                    total_videos=len(videos),
                )
                self.db.add(job)
                self.db.flush()

                allocation = allocate_output_paths(videos, output_dir)

                for index, video in enumerate(videos, start=1):
                    self.db.add(
                        SubtitleJobItem(
                            job_id=job.id,
                            index=index,
                            source_path=str(video),
                            source_name=video.name,
                            output_path=str(allocation[video]),
                            status=SubtitleJobItemStatus.PENDING,
                        )
                    )
                self.db.flush()

            logger.info(
                "字幕提取任务创建成功 | id=%s | 视频数=%s | 引擎=%s | 输出=%s",
                job.id,
                len(videos),
                params["asr"],
                output_dir,
            )
            return job
        except (BadRequestError, NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("字幕提取任务创建失败 | input=%s", payload.input_path)
            raise DatabaseError("字幕提取任务创建失败") from exc

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------

    def cancel_job(self, job_id: int) -> SubtitleJob:
        """取消任务。

        pending 任务：直接置 cancelled，工作线程认领时会跳过；
        running 任务：置 cancelled 后由执行线程在下一次 tick（≤0.5 秒）发现，
        杀掉当前子进程组、把未执行的条目标记为 skipped。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务已处于终态。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)

                if job.status in SubtitleJobStatus.TERMINAL:
                    raise ConflictError(f"任务已是终态（{job.status}），无法取消")

                job.status = SubtitleJobStatus.CANCELLED
                job.finished_at = utcnow()
                self.db.flush()

            logger.info("字幕提取任务已取消 | id=%s", job_id)
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("取消字幕提取任务失败 | id=%s", job_id)
            raise DatabaseError("取消字幕提取任务失败") from exc

    def update_remark(self, job_id: int, payload: JobRemarkUpdate) -> SubtitleJob:
        """更新任务备注（空串表示清空）。

        备注是纯用户标记，不参与状态机，因此任何状态（含运行中、终态）都能改。

        Raises:
            NotFoundError: 任务不存在。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)
                job.remark = payload.remark
                self.db.flush()

            logger.info("字幕提取任务备注已更新 | id=%s", job_id)
            return job
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("更新字幕提取任务备注失败 | id=%s", job_id)
            raise DatabaseError("更新任务备注失败") from exc

    def delete_job(self, job_id: int, *, purge_files: bool = False) -> None:
        """删除任务记录（级联删除所有条目）。

        仅允许删除终态任务。默认只删记录；purge_files=True 时把各条目登记的
        字幕文件一并删掉（释放空间是用户明确勾选的操作）。产物清理在记录
        提交之后 best-effort 执行，个别文件被占用不阻断删除。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务尚未结束。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)

                if job.status not in SubtitleJobStatus.TERMINAL:
                    raise ConflictError(f"任务尚未结束（{job.status}），请先取消后再删除")

                products = _product_paths(job) if purge_files else []
                self.db.delete(job)

            logger.info("字幕提取任务已删除 | id=%s | 清产物=%s", job_id, purge_files)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("删除字幕提取任务失败 | id=%s", job_id)
            raise DatabaseError("删除字幕提取任务失败") from exc

        _purge_products(products, job_ids=[job_id])

    def delete_jobs(self, job_ids: List[int], *, purge_files: bool = False) -> List[int]:
        """批量删除任务记录：全部成功才提交，任何一个不可删则整批回滚。

        与 delete_job 同一套边界：仅终态可删；purge_files=True 时产物一并清掉。
        错误信息带出错位的任务 ID，前端能直接告诉用户卡在哪条。

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
                    if job.status not in SubtitleJobStatus.TERMINAL:
                        raise ConflictError(
                            f"任务 #{job_id} 尚未结束（{job.status}），请先取消后再删除"
                        )
                    if purge_files:
                        products.extend(_product_paths(job))
                    self.db.delete(job)

            logger.info("字幕提取任务批量删除 | ids=%s | 清产物=%s", job_ids, purge_files)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("批量删除字幕提取任务失败 | ids=%s", job_ids)
            raise DatabaseError("批量删除任务失败") from exc

        _purge_products(products, job_ids=job_ids)
        return job_ids

    # ------------------------------------------------------------------
    # 重试
    # ------------------------------------------------------------------

    def retry_item(self, job_id: int, item_index: int) -> SubtitleJob:
        """重试单条：条目重置回 pending，任务重新入队。

        前置条件：任务已终态、且该条目是 failed 或 skipped —— 正在跑的任务没法
        重试（工作线程正拿着它），已转写成功的条目重跑没有意义。

        Raises:
            NotFoundError: 任务或条目不存在。
            ConflictError: 任务未结束，或该条目不是 failed / skipped。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)
                self._ensure_terminal_for_retry(job)
                item = self._find_item_or_404(job, item_index)
                self._ensure_item_retryable(item)
                self._reset_items_for_retry(job, [item])
                self.db.flush()

            logger.info(
                "字幕提取条目已重试入队 | job=%s | item=%s | %s",
                job_id, item_index, item.source_name,
            )
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("重试字幕提取条目失败 | job=%s | item=%s", job_id, item_index)
            raise DatabaseError("重试字幕提取条目失败") from exc

    def retry_items(self, job_id: int) -> SubtitleJob:
        """一键重试：把该任务**所有** failed / skipped 的条目一起重新入队。

        批量必须走这一个方法、不能在前端循环调 retry_item —— 第一次调用就把
        任务置回 pending，第二次会撞上「任务尚未结束」的终态校验。所以终态校验
        在循环之外只做一次，任务级字段也只在所有条目都重置完之后写一遍。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务未结束，或没有可重试的条目。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)
                self._ensure_terminal_for_retry(job)

                pending = [
                    entry
                    for entry in job.items
                    if entry.status in SubtitleJobItemStatus.RETRYABLE
                ]
                if not pending:
                    raise ConflictError(
                        f"任务 #{job_id} 没有可重试的条目（{job.failed_videos} 条失败、"
                        f"{job.skipped_videos} 条跳过）"
                    )

                self._reset_items_for_retry(job, pending)
                self.db.flush()

            logger.info(
                "字幕提取任务批量重试入队 | job=%s | 条目数=%s", job_id, len(pending)
            )
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("批量重试字幕提取任务失败 | job=%s", job_id)
            raise DatabaseError("批量重试字幕提取任务失败") from exc

    def _ensure_terminal_for_retry(self, job: SubtitleJob) -> None:
        """重试的公共前置：任务必须已经结束。

        Raises:
            ConflictError: 任务还在排队 / 正在跑。
        """
        if job.status not in SubtitleJobStatus.TERMINAL:
            raise ConflictError(f"任务尚未结束（{job.status}），无法重试")

    def _find_item_or_404(self, job: SubtitleJob, item_index: int) -> SubtitleJobItem:
        """按任务内序号找条目，找不到就 404。

        Raises:
            NotFoundError: 该序号不在任务里。
        """
        item: Optional[SubtitleJobItem] = next(
            (entry for entry in job.items if entry.index == item_index), None
        )
        if item is None:
            raise NotFoundError(f"任务条目不存在：job={job.id}, index={item_index}")
        return item

    @staticmethod
    def _ensure_item_retryable(item: SubtitleJobItem) -> None:
        """只有「没产出」的条目能重试：failed 与 skipped 都是没转写出字幕。

        Raises:
            ConflictError: 条目是 pending / running / success。
        """
        if item.status not in SubtitleJobItemStatus.RETRYABLE:
            raise ConflictError(
                f"只有失败或跳过的条目才能重试（当前状态：{item.status}）"
            )

    def _reset_items_for_retry(
        self, job: SubtitleJob, items: List[SubtitleJobItem]
    ) -> None:
        """把给定条目重置回 pending，并把任务重新入队（单条 / 批量共用）。

        三个容易踩的地方：

        1. **计数要重算，不能清零了事**。completed_videos / subtitle_count 都是
           执行器里 `+= 1` 累加出来的（subtitle_runner._finish_item），收尾的
           _finalize 只重算 failed / skipped / subtitle_count，所以这里必须按
           条目状态数一遍再写。之后重跑的条目会在执行时各自 +1，收尾时
           failed / skipped 再由 _finalize 数回来，总数自洽。
        2. **上一轮的字幕文件要删掉**。输出路径按条目定死（重名已加后缀），
           留着旧 .srt 会让「字幕存在」这个判据失真 —— 用户看到的会是上一轮的
           文本，而不是这一轮的结果。
        3. **child_pid 必须清**：它是取消 / 孤儿回收共用的唯一依据，留着旧 PID
           有误杀无关进程的风险。
        """
        for item in items:
            # 正常情况下 failed / skipped 的条目本来就没有字幕文件，这里是
            # 防御性清理（上一轮写了一半、或用户手工放进来的同名文件）。
            stale = Path(item.output_path)
            try:
                if stale.is_file():
                    stale.unlink()
            except OSError as exc:
                logger.warning(
                    "清理重试前的残留字幕失败，跳过该文件 | %s | %s", stale, exc
                )

            item.status = SubtitleJobItemStatus.PENDING
            item.subtitle_exists = False
            item.file_size = 0
            item.segment_count = 0
            item.duration_seconds = None
            item.exit_code = None
            item.elapsed_seconds = 0
            item.error_message = ""
            item.started_at = None
            item.finished_at = None

        job.status = SubtitleJobStatus.PENDING
        job.error_message = ""
        job.started_at = None
        job.finished_at = None
        job.child_pid = None
        job.current_index = 0
        job.current_video = ""
        job.current_elapsed_seconds = 0
        job.completed_videos = sum(
            1 for entry in job.items if entry.status == SubtitleJobItemStatus.SUCCESS
        )
        job.failed_videos = 0
        job.skipped_videos = 0
        job.subtitle_count = sum(1 for entry in job.items if entry.subtitle_exists)

    # ------------------------------------------------------------------
    # 产物
    # ------------------------------------------------------------------

    def list_subtitles(self, job_id: int) -> List[Dict]:
        """列出任务产出的字幕文件（按条目顺序）。

        返回的 index 是全任务范围内从 1 开始的连续序号 —— 预览接口就用这个
        序号定位文件，避免前端传路径造成任意文件读取。

        与镜头分割的 list_clips 不同，这里**不检查文件是否存在**：条目在创建
        任务时就带上了确定的输出路径，列表要展示「应该产出哪些、哪些已经好了」，
        而不是只展示已存在的那些。

        Raises:
            NotFoundError: 任务不存在。
        """
        job = self.get_job(job_id)

        files: List[Dict] = []
        for item in job.items:
            path = Path(item.output_path)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            files.append(
                {
                    "index": len(files) + 1,
                    "item_index": item.index,
                    "name": path.name,
                    "source_name": item.source_name,
                    "size_bytes": size,
                    "segment_count": item.segment_count,
                    "exists": size > 0,
                    "path": path,
                }
            )
        return files

    def get_subtitle_path(self, job_id: int, index: int) -> Tuple[Path, str, str]:
        """按全局序号定位一个字幕文件（供预览接口使用）。

        路径完全由任务记录推导，不接受任何外部传入的路径片段 —— 放开让前端
        传路径就是任意文件读取漏洞。

        Returns:
            (文件路径, 文件名, 来源视频名)

        Raises:
            NotFoundError: 任务不存在、序号越界、或文件还没生成/已被移走。
        """
        files = self.list_subtitles(job_id)
        if index < 1 or index > len(files):
            raise NotFoundError(
                f"字幕文件不存在：job={job_id}, index={index}（共 {len(files)} 份）"
            )
        entry = files[index - 1]
        path: Path = entry["path"]
        if not path.is_file():
            raise NotFoundError(f"字幕文件还没有生成，或已被移动：{entry['name']}")
        return path, entry["name"], entry["source_name"]
