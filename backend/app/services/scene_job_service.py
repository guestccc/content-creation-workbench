"""智能镜头分割任务的业务逻辑层。

职责边界：
- 本模块只管「状态机 + 查询 + 创建时的文件枚举」，完全不碰子进程；
- 真正起 vct 子进程的执行逻辑在 scene_runner.py，工作线程在 scene_job_worker.py。

任务生命周期：

    pending ──工作线程认领──> running ──全部成功──> success
       │                       │
       │                       ├──部分失败──> partial
       │                       └──全部失败──> failed
       │
       └──取消──> cancelled（running 中取消：当前条目标记后同样落到 cancelled）

终态（success / partial / failed / cancelled）只能通过删除记录清理，
不提供**自动**重试 —— 视频文件损坏是确定性错误，自动重跑只会再浪费几分钟；
但用户手动点「重试」要能做（见 retry_item / retry_items）：把 failed / skipped
的条目重置回 pending、任务重新入队 —— 取消 / 服务重启之后剩下的那几条是
skipped，没有这个入口就只能整条任务重来、成功的白跑一遍。
"""

import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import BadRequestError, ConflictError, DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.core import media_files
from app.core.scene_templates import resolve_params
from app.db.session import transaction
from app.models.content import utcnow
from app.services.fs_cleanup import remove_paths_best_effort
from app.models.scene_job import (
    SceneJob,
    SceneJobItem,
    SceneJobItemStatus,
    SceneJobMode,
    SceneJobStatus,
)
from app.schemas.common import JobRemarkUpdate
from app.schemas.scene_job import SceneJobCreate

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# 视频枚举（模块级纯函数，便于单独测试）
# --------------------------------------------------------------------------


def is_video_file(name: str) -> bool:
    """按后端配置的扩展名白名单判断是否为可处理的视频文件。

    实现在 core/media_files.py（字幕提取与换背景要用同一套规则），这里只是把
    扩展名白名单填上本功能的配置 —— 调用方（含 api/v1/fs.py）保持原样。
    """
    return media_files.is_media_file(name, settings.SCENE_INPUT_EXTENSIONS)


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
        extensions=settings.SCENE_INPUT_EXTENSIONS,
        max_files=settings.SCENE_MAX_BATCH_FILES,
    )


def _allocate_output_dirs(
    videos: List[Path], output_root: Path, *, check_existing_clips: bool
) -> Dict[Path, Path]:
    """为每条视频分配独立的输出子目录，并做预检与预建。

    目录名沿用 vct CLI 自己的默认约定 <视频名>_scenes；同一批里出现同名
    （recursive 模式下不同子目录里可能有同名文件）时追加 -2、-3 后缀。

    预建的原因：进度是按输出目录里 *_clip_*.mp4 的个数算的，目录保证是空的，
    计数才等于真实进度，不会被历史残留污染。所以 split 模式下如果目录已存在
    且已有片段，直接报错让用户换目录，而不是默默混着写。

    Args:
        videos: 视频清单。
        output_root: 输出根目录。
        check_existing_clips: split 模式传 True（已有片段则报错）；preview 模式
            输出在后端临时目录里，传 False。

    Raises:
        BadRequestError: 某个输出目录已存在切割结果，或输出目录无法创建。
    """
    allocation: Dict[Path, Path] = {}
    used_names: Dict[str, int] = {}

    for video in videos:
        base_name = f"{video.stem}_scenes"
        count = used_names.get(base_name, 0)
        used_names[base_name] = count + 1
        dir_name = base_name if count == 0 else f"{base_name}-{count + 1}"
        out_dir = output_root / dir_name

        if check_existing_clips and out_dir.is_dir() and list(out_dir.glob("*_clip_*.mp4")):
            raise BadRequestError(
                f"输出目录已存在同名切割结果：{out_dir}；请更换输出目录或先清理旧结果"
            )

        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BadRequestError(f"创建输出目录失败：{out_dir}（{exc}）") from exc

        allocation[video] = out_dir

    return allocation


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------


def _product_paths(job: SceneJob) -> List[Path]:
    """列出一条任务的产物路径（只从任务记录推导，不接受外部路径）。

    - split：每条视频一个独立输出子目录（分配时同名加 -2/-3 后缀），整棵删，
      缩略图缓存、vct.log、切点 CSV 一起清掉；
    - preview：产物在临时目录 vct-preview/job-<id>，任务独占，整棵删。

    已知边界：若任务 A 零片段失败、任务 B 之后复用了同一目录，再 purge A
    会误删 B 的产物 —— 路径即归属，两个任务撞名时无从区分，接受这个角例。
    """
    if job.mode == SceneJobMode.PREVIEW:
        return [Path(job.output_dir)]
    return [Path(item.output_dir) for item in job.items]


def _purge_products(products: List[Path], *, job_ids: List[int]) -> None:
    """best-effort 清产物：有失败的记一条汇总日志，不向上抛。"""
    if not products:
        return
    failed = remove_paths_best_effort(products)
    if failed:
        logger.warning(
            "任务产物清理有残留 | jobs=%s | 失败=%s 个路径", job_ids, len(failed)
        )


class SceneJobService:
    """镜头分割任务服务。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _get_job_or_404(self, job_id: int) -> SceneJob:
        """按 ID 取任务，不存在则抛 NotFoundError。"""
        job = self.db.get(SceneJob, job_id)
        if job is None:
            raise NotFoundError(f"镜头分割任务不存在：id={job_id}")
        return job

    def get_job(self, job_id: int) -> SceneJob:
        """按 ID 获取任务（含每个视频的明细）。"""
        try:
            return self._get_job_or_404(job_id)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("查询镜头分割任务失败 | id=%s", job_id)
            raise DatabaseError("查询镜头分割任务失败") from exc

    def list_jobs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Tuple[List[SceneJob], int]:
        """分页查询任务，按创建时间倒序。

        Returns:
            (当前页数据, 满足条件的总条数)
        """
        try:
            conditions = []
            if status:
                conditions.append(SceneJob.status == status)
            if mode:
                conditions.append(SceneJob.mode == mode)

            count_stmt = select(func.count()).select_from(SceneJob)
            list_stmt = select(SceneJob)
            for condition in conditions:
                count_stmt = count_stmt.where(condition)
                list_stmt = list_stmt.where(condition)

            total = self.db.execute(count_stmt).scalar_one()

            list_stmt = (
                list_stmt.order_by(SceneJob.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            items = list(self.db.execute(list_stmt).scalars().all())
            return items, int(total)
        except SQLAlchemyError as exc:
            logger.exception("查询镜头分割任务列表失败")
            raise DatabaseError("查询镜头分割任务列表失败") from exc

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def create_job(self, payload: SceneJobCreate) -> SceneJob:
        """创建镜头分割任务：枚举视频、预建输出目录、落库排队。

        创建成功即返回，真正的检测与切割由后台工作线程认领执行。

        Raises:
            BadRequestError: 输入路径/输出目录不合法，或目录里没有视频。
            DatabaseError: 写库失败（事务已回滚，已预建的空目录无害残留）。
        """
        videos = enumerate_videos(
            payload.input_path, recursive=payload.recursive, files=payload.files
        )
        params = resolve_params(
            payload.template,
            detector=payload.detector,
            threshold=payload.threshold,
            min_len=payload.min_len,
            copy_mode=payload.copy_mode,
        )

        # 预览模式不污染用户目录：CSV 写到后端管理的临时目录
        if payload.mode == SceneJobMode.PREVIEW:
            output_root = Path(tempfile.gettempdir()) / "vct-preview"
        else:
            # split 模式 output_dir 已在 schema 层保证非空
            output_root = Path(payload.output_dir or "")

        try:
            with transaction(self.db):
                job = SceneJob(
                    mode=payload.mode,
                    status=SceneJobStatus.PENDING,
                    input_path=payload.input_path,
                    output_dir=str(output_root),
                    recursive=payload.recursive,
                    params=params,
                    total_videos=len(videos),
                )
                self.db.add(job)
                self.db.flush()  # 先拿到 job.id，预览目录名里要用

                if payload.mode == SceneJobMode.PREVIEW:
                    # 每个任务一个独立的预览根目录，避免不同任务互相覆盖 CSV
                    output_root = output_root / f"job-{job.id}"
                    job.output_dir = str(output_root)

                allocation = _allocate_output_dirs(
                    videos,
                    output_root,
                    check_existing_clips=payload.mode == SceneJobMode.SPLIT,
                )

                for index, video in enumerate(videos, start=1):
                    self.db.add(
                        SceneJobItem(
                            job_id=job.id,
                            index=index,
                            source_path=str(video),
                            source_name=video.name,
                            output_dir=str(allocation[video]),
                            status=SceneJobItemStatus.PENDING,
                        )
                    )
                self.db.flush()

            logger.info(
                "镜头分割任务创建成功 | id=%s | mode=%s | 视频数=%s",
                job.id,
                job.mode,
                len(videos),
            )
            return job
        except (BadRequestError, NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("镜头分割任务创建失败 | input=%s", payload.input_path)
            raise DatabaseError("镜头分割任务创建失败") from exc

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------

    def cancel_job(self, job_id: int) -> SceneJob:
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

                if job.status in SceneJobStatus.TERMINAL:
                    raise ConflictError(f"任务已是终态（{job.status}），无法取消")

                job.status = SceneJobStatus.CANCELLED
                job.finished_at = utcnow()
                self.db.flush()

            logger.info("镜头分割任务已取消 | id=%s", job_id)
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("取消镜头分割任务失败 | id=%s", job_id)
            raise DatabaseError("取消镜头分割任务失败") from exc

    def update_remark(self, job_id: int, payload: JobRemarkUpdate) -> SceneJob:
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

            logger.info("镜头分割任务备注已更新 | id=%s", job_id)
            return job
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("更新镜头分割任务备注失败 | id=%s", job_id)
            raise DatabaseError("更新任务备注失败") from exc

    def delete_job(self, job_id: int, *, purge_files: bool = False) -> None:
        """删除任务记录（级联删除所有条目）。

        仅允许删除终态任务。默认只删记录；purge_files=True 时把任务产物
        （每条视频的输出目录 / 预览的临时目录）一并清掉 —— 用户明确勾选的
        释放空间操作。产物清理在记录提交之后 best-effort 执行，个别文件
        被占用不阻断删除。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务尚未结束。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)

                if job.status not in SceneJobStatus.TERMINAL:
                    raise ConflictError(
                        f"任务尚未结束（{job.status}），请先取消后再删除"
                    )

                products = _product_paths(job) if purge_files else []
                self.db.delete(job)

            logger.info("镜头分割任务已删除 | id=%s | 清产物=%s", job_id, purge_files)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("删除镜头分割任务失败 | id=%s", job_id)
            raise DatabaseError("删除镜头分割任务失败") from exc

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
                    if job.status not in SceneJobStatus.TERMINAL:
                        raise ConflictError(
                            f"任务 #{job_id} 尚未结束（{job.status}），请先取消后再删除"
                        )
                    if purge_files:
                        products.extend(_product_paths(job))
                    self.db.delete(job)

            logger.info("镜头分割任务批量删除 | ids=%s | 清产物=%s", job_ids, purge_files)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("批量删除镜头分割任务失败 | ids=%s", job_ids)
            raise DatabaseError("批量删除任务失败") from exc

        _purge_products(products, job_ids=job_ids)
        return job_ids

    def retry_item(self, job_id: int, item_index: int) -> SceneJob:
        """重试单条「没产出」的视频：条目重置回 pending，任务重新入队。

        前置条件：任务已终态、且该条目是 failed 或 skipped —— 正在跑的任务没法
        重试（工作线程正拿着它），已成功的条目重跑没有意义（要重跑就新建任务）。

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
                "镜头分割条目已重试入队 | job=%s | item=%s | %s",
                job_id, item_index, item.source_name,
            )
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("重试镜头分割条目失败 | job=%s | item=%s", job_id, item_index)
            raise DatabaseError("重试镜头分割条目失败") from exc

    def retry_items(self, job_id: int) -> SceneJob:
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
                    if entry.status in SceneJobItemStatus.RETRYABLE
                ]
                if not pending:
                    raise ConflictError(
                        f"任务 #{job_id} 没有可重试的条目（{job.failed_videos} 条失败、"
                        f"{job.skipped_videos} 条跳过）"
                    )

                self._reset_items_for_retry(job, pending)
                self.db.flush()

            logger.info(
                "镜头分割任务批量重试入队 | job=%s | 条目数=%s", job_id, len(pending)
            )
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("批量重试镜头分割任务失败 | job=%s", job_id)
            raise DatabaseError("批量重试镜头分割任务失败") from exc

    def _ensure_terminal_for_retry(self, job: SceneJob) -> None:
        """重试的公共前置：任务必须已经结束。

        Raises:
            ConflictError: 任务还在排队 / 正在跑。
        """
        if job.status not in SceneJobStatus.TERMINAL:
            raise ConflictError(f"任务尚未结束（{job.status}），无法重试")

    def _find_item_or_404(self, job: SceneJob, item_index: int) -> SceneJobItem:
        """按任务内序号找条目，找不到就 404。

        Raises:
            NotFoundError: 该序号不在任务里。
        """
        item: Optional[SceneJobItem] = next(
            (entry for entry in job.items if entry.index == item_index), None
        )
        if item is None:
            raise NotFoundError(f"任务条目不存在：job={job.id}, index={item_index}")
        return item

    @staticmethod
    def _ensure_item_retryable(item: SceneJobItem) -> None:
        """只有「没产出」的条目能重试：failed 与 skipped 都是没切出片段。

        skipped 是取消 / 服务重启时压根没轮到的那些 —— 重启恢复的提示语里写着
        「可直接重新发起」，只放宽 failed 的话那句话兑不了现。

        Raises:
            ConflictError: 条目是 pending / running / success。
        """
        if item.status not in SceneJobItemStatus.RETRYABLE:
            raise ConflictError(
                f"只有失败或跳过的条目才能重试（当前状态：{item.status}）"
            )

    def _reset_items_for_retry(self, job: SceneJob, items: List[SceneJobItem]) -> None:
        """把给定条目重置回 pending，并把任务重新入队（单条 / 批量共用）。

        条目的输出目录会先清空：进度按目录里片段文件数统计、结果按目录里片段
        文件数收集（见 scene_runner），上次的残留会把重跑的进度和产物数全部污染。
        目录本身归属该条目独占，清空不会误伤别的视频。

        计数口径：completed_videos 按条目状态重数（它是执行器 += 1 出来的、
        收尾的 _finalize 不会重算），failed / skipped 清零 —— 它们会在重跑过程中
        由执行器按条目重新累计，收尾时 _finalize 再按条目状态重算一遍。
        """
        for item in items:
            out_dir = Path(item.output_dir)
            if out_dir.is_dir():
                for stale in out_dir.iterdir():
                    try:
                        if stale.is_dir():
                            shutil.rmtree(stale, ignore_errors=True)
                        else:
                            stale.unlink()
                    except OSError as exc:
                        logger.warning(
                            "清理重试前的残留产物失败，跳过该文件 | %s | %s", stale, exc
                        )
            out_dir.mkdir(parents=True, exist_ok=True)

            item.status = SceneJobItemStatus.PENDING
            item.error_message = ""
            item.exit_code = None
            item.started_at = None
            item.finished_at = None
            item.elapsed_seconds = 0
            item.scene_count = 0
            item.scenes = None
            item.clip_count = 0
            item.clip_names = []
            item.failed_clip_count = 0
            item.single_shot = False

        job.status = SceneJobStatus.PENDING
        job.error_message = ""
        job.started_at = None
        job.finished_at = None
        job.child_pid = None
        job.current_index = 0
        job.current_video = ""
        job.current_clips = 0
        job.current_clip_names = []
        job.current_phase = ""
        job.current_total_clips = 0
        job.completed_videos = sum(
            1 for entry in job.items if entry.status == SceneJobItemStatus.SUCCESS
        )
        job.failed_videos = 0
        job.skipped_videos = 0

    # ------------------------------------------------------------------
    # 结果汇总
    # ------------------------------------------------------------------

    def get_scenes_summary(self, job_id: int) -> Dict:
        """汇总预览模式下所有视频的切点，供「预览切点」结果区展示。

        Raises:
            NotFoundError: 任务不存在。
        """
        job = self.get_job(job_id)

        durations: List[float] = []
        for item in job.items:
            for scene in item.scenes or []:
                duration = scene.get("duration")
                if isinstance(duration, (int, float)):
                    durations.append(float(duration))

        return {
            "job_id": job.id,
            "status": job.status,
            "total_scenes": sum(item.scene_count for item in job.items),
            "total_duration": round(sum(durations), 3),
            "shortest": round(min(durations), 3) if durations else None,
            "longest": round(max(durations), 3) if durations else None,
            "average": (
                round(sum(durations) / len(durations), 3) if durations else None
            ),
            "items": job.items,
        }

    def list_clips(self, job_id: int) -> List[Dict]:
        """列出任务切出的所有片段（跨视频按条目顺序、再按文件名排序）。

        返回的 index 是全任务范围内从 1 开始的连续序号 —— 缩略图接口
        就用这个序号定位文件，避免前端传路径造成任意文件读取。

        每一条还带上 item_index（它属于哪一条视频）—— 前端要按视频分组的
        时候只能靠这个：递归模式下两条同名视频是很正常的，拿文件名去归组
        会把它们混在一起。

        Raises:
            NotFoundError: 任务不存在。
        """
        job = self.get_job(job_id)

        clips: List[Dict] = []
        for item in job.items:
            for name in sorted(item.clip_names or []):
                file_path = Path(item.output_dir) / name
                try:
                    size = file_path.stat().st_size
                except OSError:
                    # 文件可能被用户在磁盘上挪走了：仍然列出，大小记 0
                    size = 0
                clips.append(
                    {
                        "index": len(clips) + 1,
                        "item_index": item.index,
                        "name": name,
                        "source_name": item.source_name,
                        "size_bytes": size,
                        # 切分不改分辨率，片段与源视频同规格：卡片比例直接用源视频的宽高
                        "width": item.width,
                        "height": item.height,
                        "path": file_path,
                    }
                )
        return clips

    def get_clip_path(self, job_id: int, clip_index: int) -> Tuple[Path, str]:
        """按全局序号定位一个片段文件（供缩略图接口使用）。

        路径完全由任务记录推导，不接受任何外部传入的路径片段。

        Raises:
            NotFoundError: 任务不存在或序号越界或文件已被移走。
        """
        clips = self.list_clips(job_id)
        if clip_index < 1 or clip_index > len(clips):
            raise NotFoundError(
                f"片段不存在：job={job_id}, index={clip_index}（共 {len(clips)} 个片段）"
            )
        clip = clips[clip_index - 1]
        path: Path = clip["path"]
        if not path.is_file():
            raise NotFoundError(f"片段文件已被移动或删除：{clip['name']}")
        return path, clip["name"]
