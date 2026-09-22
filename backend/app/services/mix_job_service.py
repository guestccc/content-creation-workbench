"""智能混剪任务的业务逻辑层。

职责边界与 scene_job_service.py 完全一致：
- 本模块只管「状态机 + 查询 + 创建时的素材校验」，完全不碰子进程；
- 真正起 ffmpeg 的执行逻辑在 mix_runner.py，工作线程在 mix_job_worker.py。

任务生命周期：

    pending ──工作线程认领──> running ──全部成功──> success
       │                       │
       │                       ├──部分成片失败──> partial
       │                       └──全部失败──> failed
       │
       └──取消──> cancelled

创建时的校验（都返回可读的中文错误，不做「点了才报错」）：
- 三个列表都非空、clip id 必须在当前素材库扫描结果里存在；
- 每个列表条数不超过 MIX_MAX_CLIPS_PER_LIST；
- 输出条数在 1..MIX_MAX_OUTPUTS 且不超过中间段排列数（K!）。

素材来自用户自己添加的素材目录（mix_library 管注册表与扫描），
任务落库存的是**素材绝对路径** —— 任务一旦创建就与注册表解耦，
用户中途移除某个目录也不影响已排队的任务。
"""

import random
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import BadRequestError, ConflictError, DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.core.materials import OUTPUT, subdir
from app.db.session import transaction
from app.models.content import utcnow
from app.models.mix_job import (
    MixJob,
    MixJobItem,
    MixJobStatus,
    MixOutputStatus,
)
from app.services.mix_library import (
    add_source,
    remove_source,
    resolve_clips,
    scan_library,
)
from app.services.mix_runner import middle_permutation_limit, plan_outputs
from app.services.fs_cleanup import remove_paths_best_effort
from app.schemas.common import JobRemarkUpdate
from app.schemas.mix_job import MixJobCreate

logger = get_logger(__name__)


def _product_paths(job: MixJob) -> List[Path]:
    """列出一条任务的产物路径（只从任务记录推导，不接受外部路径）。

    整个输出目录（mix-<时间戳>，创建时 exist_ok=False 保证任务独占）
    整棵删 —— 比逐条成片删更干净，失败的半成品也不会留下空目录。
    """
    return [Path(job.output_dir)]


def _purge_products(products: List[Path], *, job_ids: List[int]) -> None:
    """best-effort 清产物：有失败的记一条汇总日志，不向上抛。"""
    if not products:
        return
    failed = remove_paths_best_effort(products)
    if failed:
        logger.warning(
            "任务产物清理有残留 | jobs=%s | 失败=%s 个路径", job_ids, len(failed)
        )


class MixJobService:
    """智能混剪任务服务。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    # ------------------------------------------------------------------
    # 素材目录与素材库
    # ------------------------------------------------------------------

    def get_library(self) -> Dict:
        """扫描所有已添加的素材目录，并补上每个片段的缩略图/播放 URL。"""
        data = scan_library()
        clips = []
        for clip in data["clips"]:
            clips.append(
                {
                    **clip,
                    "thumb_url": f"/api/v1/mix/library/clips/{clip['id']}/thumb",
                    "video_url": f"/api/v1/mix/library/clips/{clip['id']}/video",
                }
            )
        return {**data, "clips": clips}

    def add_source(self, path: str) -> Tuple[Dict, bool]:
        """添加一个素材目录（路径非法/不存在时抛可读的中文错误）。

        Returns:
            (目录信息, 是否新建)。重复添加同一个目录是幂等的。
        """
        try:
            return add_source(path)
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc

    def remove_source(self, source_id: str) -> None:
        """把素材目录移出列表（磁盘上的文件一律不动）。"""
        try:
            removed = remove_source(source_id)
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
        if not removed:
            raise NotFoundError(f"素材目录不存在：{source_id}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _get_job_or_404(self, job_id: int) -> MixJob:
        """按 ID 取任务，不存在则抛 NotFoundError。"""
        job = self.db.get(MixJob, job_id)
        if job is None:
            raise NotFoundError(f"混剪任务不存在：id={job_id}")
        return job

    def get_job(self, job_id: int) -> MixJob:
        """按 ID 获取任务（含每条成片的明细），前端轮询进度也用它。"""
        try:
            return self._get_job_or_404(job_id)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("查询混剪任务失败 | id=%s", job_id)
            raise DatabaseError("查询混剪任务失败") from exc

    def list_jobs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None,
    ) -> Tuple[List[MixJob], int]:
        """分页查询任务，按创建时间倒序。"""
        try:
            conditions = []
            if status:
                conditions.append(MixJob.status == status)

            count_stmt = select(func.count()).select_from(MixJob)
            list_stmt = select(MixJob)
            for condition in conditions:
                count_stmt = count_stmt.where(condition)
                list_stmt = list_stmt.where(condition)

            total = self.db.execute(count_stmt).scalar_one()
            list_stmt = (
                list_stmt.order_by(MixJob.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            items = list(self.db.execute(list_stmt).scalars().all())
            return items, int(total)
        except SQLAlchemyError as exc:
            logger.exception("查询混剪任务列表失败")
            raise DatabaseError("查询混剪任务列表失败") from exc

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def create_job(self, payload: MixJobCreate) -> MixJob:
        """创建混剪任务：校验素材、预建输出目录、落库排队。

        Raises:
            BadRequestError: 列表为空 / clip id 不存在 / 超限 / 排列数不够 / 输出目录不合法。
            DatabaseError: 写库失败。
        """
        max_per_list = settings.MIX_MAX_CLIPS_PER_LIST
        for label, items in (("开头", payload.opening), ("中间", payload.middle), ("结尾", payload.ending)):
            if not items:
                raise BadRequestError(f"{label}列表不能为空")
            if len(items) > max_per_list:
                raise BadRequestError(f"{label}列表最多 {max_per_list} 条，当前 {len(items)} 条")
            if len(set(items)) != len(items):
                raise BadRequestError(f"{label}列表内有重复素材")

        # 批量解析 clip id：只扫一次目录；扫不到的 id 就是不存在或已被删除
        all_ids = list(dict.fromkeys(payload.opening + payload.middle + payload.ending))
        resolved = resolve_clips(all_ids)
        missing = [cid for cid in all_ids if cid not in resolved]
        if missing:
            raise BadRequestError(f"素材不存在或已被移动：{len(missing)} 个片段无法定位")

        # 中间段排列数上限：超过必然重复，宁可拒绝也不产出重样的成片
        limit = middle_permutation_limit(len(payload.middle))
        if payload.count > limit:
            raise BadRequestError(
                f"中间选了 {len(payload.middle)} 条，最多只能排出 {limit} 种不同顺序"
            )

        output_root = Path(payload.output_dir)
        if not output_root.is_dir():
            raise BadRequestError(f"输出目录不存在或不是目录：{payload.output_dir}")

        # 存素材绝对路径：素材可以来自任意目录，没有共同的「根」可相对；
        # 任务创建后就与素材目录注册表解耦，用户中途移除目录不影响已排队的任务。
        opening_files = [str(resolved[cid]) for cid in payload.opening]
        middle_files = [str(resolved[cid]) for cid in payload.middle]
        ending_files = [str(resolved[cid]) for cid in payload.ending]

        seed = random.randint(1, 2**31 - 1)
        plans = plan_outputs(opening_files, middle_files, ending_files, payload.count, seed)

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output_dir = output_root / f"mix-{timestamp}"
        try:
            output_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            # 同一秒撞名：补一个随机后缀
            output_dir = output_root / f"mix-{timestamp}-{random.randint(100, 999)}"
            output_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise BadRequestError(f"创建输出目录失败：{output_dir}（{exc}）") from exc

        try:
            with transaction(self.db):
                job = MixJob(
                    status=MixJobStatus.PENDING,
                    opening=opening_files,
                    middle=middle_files,
                    ending=ending_files,
                    count=payload.count,
                    output_dir=str(output_dir),
                    seed=seed,
                    target={},
                    total_outputs=payload.count,
                )
                self.db.add(job)
                self.db.flush()

                for index, plan in enumerate(plans, start=1):
                    self.db.add(
                        MixJobItem(
                            job_id=job.id,
                            index=index,
                            order=plan.order,
                            status=MixOutputStatus.PENDING,
                        )
                    )
                self.db.flush()

            logger.info(
                "混剪任务创建成功 | id=%s | 成片数=%s | 种子=%s | 输出=%s",
                job.id, job.count, seed, output_dir,
            )
            return job
        except SQLAlchemyError as exc:
            logger.exception("混剪任务创建失败")
            raise DatabaseError("混剪任务创建失败") from exc

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------

    def cancel_job(self, job_id: int) -> MixJob:
        """取消任务（与镜头分割同一套做法）。"""
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)
                if job.status in MixJobStatus.TERMINAL:
                    raise ConflictError(f"任务已是终态（{job.status}），无法取消")
                job.status = MixJobStatus.CANCELLED
                job.finished_at = utcnow()
                self.db.flush()
            logger.info("混剪任务已取消 | id=%s", job_id)
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("取消混剪任务失败 | id=%s", job_id)
            raise DatabaseError("取消混剪任务失败") from exc

    def delete_job(self, job_id: int, *, purge_files: bool = False) -> None:
        """删除任务记录（级联删除成片条目）。

        仅允许删除终态任务。默认只删记录；purge_files=True 时把任务的输出
        目录整棵删掉（释放空间是用户明确勾选的操作）。产物清理在记录提交
        之后 best-effort 执行，个别文件被占用不阻断删除。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务尚未结束。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)
                if job.status not in MixJobStatus.TERMINAL:
                    raise ConflictError(f"任务尚未结束（{job.status}），请先取消后再删除")
                products = _product_paths(job) if purge_files else []
                self.db.delete(job)
            logger.info("混剪任务已删除 | id=%s | 清产物=%s", job_id, purge_files)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("删除混剪任务失败 | id=%s", job_id)
            raise DatabaseError("删除混剪任务失败") from exc

        _purge_products(products, job_ids=[job_id])

    def delete_jobs(self, job_ids: List[int], *, purge_files: bool = False) -> List[int]:
        """批量删除任务记录：全部成功才提交，任何一个不可删则整批回滚。

        与 delete_job 同一套边界：仅终态可删；purge_files=True 时输出目录
        一并清掉。错误信息带出错位的任务 ID，前端能直接告诉用户卡在哪条。

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
                    if job.status not in MixJobStatus.TERMINAL:
                        raise ConflictError(
                            f"任务 #{job_id} 尚未结束（{job.status}），请先取消后再删除"
                        )
                    if purge_files:
                        products.extend(_product_paths(job))
                    self.db.delete(job)
            logger.info("混剪任务批量删除 | ids=%s | 清产物=%s", job_ids, purge_files)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("批量删除混剪任务失败 | ids=%s", job_ids)
            raise DatabaseError("批量删除任务失败") from exc

        _purge_products(products, job_ids=job_ids)
        return job_ids

    # ------------------------------------------------------------------
    # 编辑
    # ------------------------------------------------------------------

    def update_remark(self, job_id: int, payload: JobRemarkUpdate) -> MixJob:
        """更新任务备注（空串表示清空）。

        备注是纯用户标记，不参与状态机，任何状态下都允许改。

        Raises:
            NotFoundError: 任务不存在。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self.db.get(MixJob, job_id)
                if job is None:
                    raise NotFoundError(f"混剪任务不存在：id={job_id}")
                job.remark = payload.remark
                self.db.flush()

            logger.info("混剪任务备注已更新 | id=%s", job_id)
            return job
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("更新混剪任务备注失败 | id=%s", job_id)
            raise DatabaseError("更新任务备注失败") from exc

    # ------------------------------------------------------------------
    # 重试
    # ------------------------------------------------------------------

    def retry_item(self, job_id: int, item_index: int) -> MixJob:
        """重试单条成片：条目重置回 pending，任务重新入队。

        前置条件：任务已终态、且该条是 failed 或 skipped —— 正在跑的任务没法
        重试（工作线程正拿着它），已拼好的成片重跑没有意义。

        ⚠️ 重试会**重新归一化全部片段**。失败收尾时 `_cleanup_tmp` 会把
        `materials/output/.tmp/mix_<id>/norm/` 整棵删掉（只留 ffmpeg 日志），
        而 Pass 1 是耗时大头 —— 所以一条 30 秒的成片重试可能要等上几分钟。
        这是正确性上的取舍（归一化产物没了就只能重做），但别让用户以为卡住了。

        Raises:
            NotFoundError: 任务或成片不存在。
            ConflictError: 任务未结束，或该条不是 failed / skipped。
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
                "混剪成片已重试入队 | job=%s | item=%s", job_id, item_index
            )
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("重试混剪成片失败 | job=%s | item=%s", job_id, item_index)
            raise DatabaseError("重试混剪成片失败") from exc

    def retry_items(self, job_id: int) -> MixJob:
        """一键重试：把该任务**所有** failed / skipped 的成片一起重新入队。

        批量必须走这一个方法、不能在前端循环调 retry_item —— 第一次调用就把
        任务置回 pending，第二次会撞上「任务尚未结束」的终态校验。所以终态校验
        在循环之外只做一次，任务级字段也只在所有条目都重置完之后写一遍。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务未结束，或没有可重试的成片。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)
                self._ensure_terminal_for_retry(job)

                pending = [
                    entry
                    for entry in job.outputs
                    if entry.status in MixOutputStatus.RETRYABLE
                ]
                if not pending:
                    raise ConflictError(
                        f"任务 #{job_id} 没有可重试的成片（{job.failed_outputs} 条失败、"
                        f"{job.skipped_outputs} 条跳过）"
                    )

                self._reset_items_for_retry(job, pending)
                self.db.flush()

            logger.info(
                "混剪任务批量重试入队 | job=%s | 成片数=%s", job_id, len(pending)
            )
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("批量重试混剪任务失败 | job=%s", job_id)
            raise DatabaseError("批量重试混剪任务失败") from exc

    def _ensure_terminal_for_retry(self, job: MixJob) -> None:
        """重试的公共前置：任务必须已经结束。

        Raises:
            ConflictError: 任务还在排队 / 正在跑。
        """
        if job.status not in MixJobStatus.TERMINAL:
            raise ConflictError(f"任务尚未结束（{job.status}），无法重试")

    def _find_item_or_404(self, job: MixJob, item_index: int) -> MixJobItem:
        """按成片序号找条目，找不到就 404。

        Raises:
            NotFoundError: 该序号不在任务里。
        """
        item: Optional[MixJobItem] = next(
            (entry for entry in job.outputs if entry.index == item_index), None
        )
        if item is None:
            raise NotFoundError(f"任务条目不存在：job={job.id}, index={item_index}")
        return item

    @staticmethod
    def _ensure_item_retryable(item: MixJobItem) -> None:
        """只有「没产出」的成片能重试。

        skipped 在这里有两种来源：任务被取消，或它依赖的片段归一化失败了 ——
        两种都是「没有成片」，而且重试会把整个归一化阶段重做一遍，第二种也能
        真正救回来。

        Raises:
            ConflictError: 该条是 pending / running / success。
        """
        if item.status not in MixOutputStatus.RETRYABLE:
            raise ConflictError(
                f"只有失败或跳过的成片才能重试（当前状态：{item.status}）"
            )

    def _reset_items_for_retry(self, job: MixJob, items: List[MixJobItem]) -> None:
        """把给定成片重置回 pending，并把任务重新入队（单条 / 批量共用）。

        四个容易踩的地方：

        1. **计数要重算，不能清零了事**。completed_outputs 是执行器里 `+= 1`
           累加出来的（mix_runner._finish_item / _skip_pending_outputs），收尾的
           _finalize 只重算 failed / skipped（并按 total_outputs 反推成功数），
           所以这里必须按条目状态数一遍再写。之后重跑的成片会在执行时各自 +1，
           收尾时 failed / skipped 再由 _finalize 数回来，总数自洽。
        2. **`order` 绝不能动**：它是该条成片的素材顺序（含这个 task seed 打乱
           后的中间段），改了就等于换了条成片，与「重试」不是一回事。
        3. **上一轮的成片文件与 concat 半成品要删掉**。输出路径按序号定死
           （`{index:02d}.mp4` / `.partial.mp4`），留着旧文件会让「成片存在」
           这个判据失真 —— 用户看到的会是上一轮的结果。
        4. **child_pid 必须清**：它是取消 / 孤儿回收共用的唯一依据，留着旧 PID
           有误杀无关进程的风险。
        """
        for item in items:
            # 正常情况下 failed / skipped 的成片本来就没有文件，这里是防御性清理
            # （上一轮写了一半、或取消时恰好留下 .partial.mp4）。
            out_dir = Path(job.output_dir)
            for stale in (
                out_dir / f"{item.index:02d}.mp4",
                out_dir / f"{item.index:02d}.partial.mp4",
            ):
                try:
                    if stale.is_file():
                        stale.unlink()
                except OSError as exc:
                    logger.warning(
                        "清理重试前的残留成片失败，跳过该文件 | %s | %s", stale, exc
                    )

            item.status = MixOutputStatus.PENDING
            item.output_path = ""
            item.output_name = ""
            item.duration_seconds = None
            item.size_bytes = 0
            item.exit_code = None
            item.elapsed_seconds = 0
            item.error_message = ""
            item.started_at = None
            item.finished_at = None

        job.status = MixJobStatus.PENDING
        job.error_message = ""
        job.started_at = None
        job.finished_at = None
        job.child_pid = None
        job.current_index = 0
        job.current_clip = ""
        job.current_phase = ""
        job.progress_percent = 0.0
        # total_clips / done_clips 由 Pass 1 开头自己重算（mix_runner 会按
        # 去重后的片段清单重新写这两个字段），这里不碰。
        job.completed_outputs = sum(
            1 for entry in job.outputs if entry.status == MixOutputStatus.SUCCESS
        )
        job.failed_outputs = 0
        job.skipped_outputs = 0

    # ------------------------------------------------------------------
    # 成片定位（供播放/封面接口用；路径完全由任务记录推导）
    # ------------------------------------------------------------------

    def get_output_path(self, job_id: int, output_index: int) -> Tuple[Path, str]:
        """按序号定位一条成片文件。

        Raises:
            NotFoundError: 任务不存在 / 序号越界 / 该条未成功 / 文件已被移走。
        """
        job = self.get_job(job_id)
        item = next((it for it in job.outputs if it.index == output_index), None)
        if item is None:
            raise NotFoundError(f"成片不存在：job={job_id}, index={output_index}")
        if item.status != MixOutputStatus.SUCCESS or not item.output_path:
            raise NotFoundError(f"该条成片尚未产出（状态：{item.status}）")
        path = Path(item.output_path)
        if not path.is_file():
            raise NotFoundError(f"成片文件已被移动或删除：{item.output_name}")
        return path, item.output_name
