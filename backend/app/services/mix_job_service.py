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
