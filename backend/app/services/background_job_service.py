"""一键换背景任务的业务逻辑层。

职责边界（与 subtitle_job_service.py 一致）：
- 本模块只管「状态机 + 查询 + 创建时的文件枚举与产物路径分配」，完全不碰算法；
- 真正抠图 / 合成的执行逻辑在 background_runner.py，工作线程在
  background_job_worker.py。

任务生命周期：

    pending ──工作线程认领──> running ──全部成功──> success
       │                       │
       │                       ├──部分失败──> partial
       │                       └──全部失败──> failed
       │
       └──取消──> cancelled（running 中取消：未开始的张标记 skipped）

终态（success / partial / failed / cancelled）只能通过删除记录清理。
不提供**自动**重试：图片损坏、超出像素上限都是确定性错误，自动重跑只会再烧一遍
内存。但用户手动点「重试」要能做（见 retry_item / retry_items）—— 取消 / 服务重启
之后剩下的那几张是 skipped，没有这个入口就只能整条任务重来、成功的白跑一遍。

**产物结构**：每次任务独占一个 `background-<时间戳>/` 子目录，里面是每张原图
一个 `<原名>.png`。删除任务时 purge_files=True 清掉的就是这个目录 ——
路径只从任务记录推导，不接受外部传入。
"""

import random
import time
from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core import media_files
from app.core.config import settings
from app.core.exceptions import BadRequestError, ConflictError, DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.core.materials import BACKGROUND, subdir
from app.core.output_paths import allocate_unique_paths
from app.db.session import transaction
from app.models.background_job import (
    BackgroundJob,
    BackgroundJobItem,
    BackgroundJobItemStatus,
    BackgroundJobStatus,
)
from app.models.content import utcnow
# 跨域只 import **模型**，不注入对方的 service（同 finalcut_render_service 的做法）：
# 这里要的只是「这个 id 存不存在」，没有任何抓取域的业务规则要复用。
from app.models.crawl_job import CrawlJob
from app.schemas.background_job import BackgroundJobCreate
from app.schemas.common import JobRemarkUpdate
from app.services.fs_cleanup import remove_paths_best_effort

logger = get_logger(__name__)

#: 产物扩展名。恒定 PNG：抠图结果是带 alpha 的，jpg 存不了透明通道，
#: 存下去就是一张黑底（或白底）图，用户拿到手才发现。
OUTPUT_SUFFIX = ".png"


def is_image_file(name: str) -> bool:
    """按后端配置的扩展名白名单判断是否为可处理的图片文件。

    用的是 **BACKGROUND_INPUT_EXTENSIONS**（算法侧白名单，Pillow 读得出来就算），
    不是镜头分割/字幕提取共用的 SCENE_INPUT_EXTENSIONS —— 两者混用会让那两个
    页面把图片列成视频。见 core/media_files.py 的说明。
    """
    return media_files.is_media_file(name, settings.BACKGROUND_INPUT_EXTENSIONS)


def enumerate_images(
    input_path: str, *, files: Optional[List[str]] = None
) -> List[Path]:
    """把输入路径展开成一份确定的原图清单（白名单与批量上限取本功能的配置）。

    规则与异常见 core/media_files.enumerate_images —— 那里是唯一实现。
    不递归子目录：换背景是「挑一批图批量处理」，用户勾选的就是眼前这一层，
    递归会把子目录里没打算处理的图也卷进来（本页也没有递归开关）。
    """
    return media_files.enumerate_images(
        input_path,
        recursive=False,
        files=files,
        extensions=settings.BACKGROUND_INPUT_EXTENSIONS,
        max_files=settings.BACKGROUND_MAX_BATCH_FILES,
    )


def allocate_output_paths(images: List[Path], output_dir: Path) -> dict:
    """为每张原图分配一个产物 PNG 路径，重名的加 -2 / -3 后缀。

    允许目标目录里已有同名文件（分批跑同一个目录是常见用法），所以不报错、
    换个名字，谁都不覆盖谁。编号规则本身在 core/output_paths.py。

    Raises:
        BadRequestError: 输出目录建不出来（权限、只读盘）。
    """
    return allocate_unique_paths(
        images,
        output_dir,
        lambda image, count: (
            f"{image.stem}{OUTPUT_SUFFIX}"
            if count == 1
            else f"{image.stem}-{count}{OUTPUT_SUFFIX}"
        ),
    )


def _product_paths(job: BackgroundJob) -> List[Path]:
    """列出一条任务的产物路径（只从任务记录推导，不接受外部路径）。

    返回**整个任务目录**而不是逐个 PNG：换背景的产物是任务独占的一个目录
    （创建时 exist_ok=False），整棵删干净才不会留下一个空壳目录。路径为空
    （老记录、手工改过库）时返回空清单，清理函数会静默跳过。
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


class BackgroundJobService:
    """一键换背景任务服务。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _get_job_or_404(self, job_id: int) -> BackgroundJob:
        """按 ID 取任务，不存在则抛 NotFoundError。"""
        job = self.db.get(BackgroundJob, job_id)
        if job is None:
            raise NotFoundError(f"换背景任务不存在：id={job_id}")
        return job

    def get_job(self, job_id: int) -> BackgroundJob:
        """按 ID 获取任务（含每张原图的明细）。"""
        try:
            return self._get_job_or_404(job_id)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("查询换背景任务失败 | id=%s", job_id)
            raise DatabaseError("查询换背景任务失败") from exc

    def list_jobs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None,
        source_crawl_job_id: Optional[int] = None,
    ) -> Tuple[List[BackgroundJob], int]:
        """分页查询任务，按创建时间倒序（即 ID 倒序）。

        Args:
            source_crawl_job_id: 只列出来自该素材抓取任务的那些（素材抓取页
                用它把派生任务挂回原任务的笔记行上）。None = 不过滤。

        Returns:
            (当前页数据, 满足条件的总条数)
        """
        try:
            conditions = []
            if status:
                conditions.append(BackgroundJob.status == status)
            # 0 不是合法 id：判 None 而不是判真值，免得以后有人传 0 时
            # 悄悄退化成「不过滤、返回全部」
            if source_crawl_job_id is not None:
                conditions.append(
                    BackgroundJob.source_crawl_job_id == source_crawl_job_id
                )

            count_stmt = select(func.count()).select_from(BackgroundJob)
            list_stmt = select(BackgroundJob)
            for condition in conditions:
                count_stmt = count_stmt.where(condition)
                list_stmt = list_stmt.where(condition)

            total = self.db.execute(count_stmt).scalar_one()

            list_stmt = (
                list_stmt.order_by(BackgroundJob.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            items = list(self.db.execute(list_stmt).scalars().all())
            return items, int(total)
        except SQLAlchemyError as exc:
            logger.exception("查询换背景任务列表失败")
            raise DatabaseError("查询换背景任务列表失败") from exc

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def create_job(self, payload: BackgroundJobCreate) -> BackgroundJob:
        """创建换背景任务：枚举原图、分配产物路径、落库排队。

        所有校验都在这里一次性做完（背景图读得出来吗、原图清单非空吗、输出目录
        建得出来吗），把错误挡在用户点击的那一刻；真正耗时的抠图由后台工作线程
        认领执行。

        Raises:
            BadRequestError: 背景图不存在 / 后缀不在白名单 / 输入路径不合法 /
                目录里没有图片 / 输出目录建不出来 / 来源素材抓取任务不存在。
            DatabaseError: 写库失败（事务已回滚）。
        """
        background = Path(payload.background_path)
        if not background.is_file():
            raise BadRequestError(f"背景图不存在：{payload.background_path}")
        if not is_image_file(background.name):
            allowed = "、".join(settings.BACKGROUND_INPUT_EXTENSIONS)
            raise BadRequestError(
                f"背景图的格式不受支持：{background.name}（支持：{allowed}）"
            )

        images = enumerate_images(payload.input_path, files=payload.files)

        # 来源任务必须存在 —— 但**不校验**「这条笔记是否属于该任务」：抓取产物
        # 随时可能被清掉（删除任务时勾了清产物），那种校验只会造出「明明有来源
        # 却建不出来」。这里只挡住「传了一个根本不存在的任务 id」。
        # 位置必须在下面建输出目录之前：晚于 mkdir 会给用户留一个空的
        # background-<时间戳>/ 目录。
        if payload.source_crawl_job_id is not None:
            source_job = self.db.get(CrawlJob, payload.source_crawl_job_id)
            if source_job is None:
                raise BadRequestError(
                    f"来源素材抓取任务不存在：id={payload.source_crawl_job_id}"
                )

        if payload.output_dir:
            output_root = Path(payload.output_dir)
            if not output_root.is_dir():
                raise BadRequestError(f"输出目录不存在或不是目录：{payload.output_dir}")
        else:
            output_root = subdir(BACKGROUND)
            output_root.mkdir(parents=True, exist_ok=True)

        # 每次任务独占一个子目录：产物不互相覆盖，删任务时整棵清掉也不会误伤
        # 别的任务（与一键成品的 finalcut-<时间戳>/ 同一套结构）。
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output_dir = output_root / f"{settings.BACKGROUND_OUTPUT_PREFIX}{timestamp}"
        try:
            output_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            # 同一秒撞名：补一个随机后缀
            output_dir = output_root / (
                f"{settings.BACKGROUND_OUTPUT_PREFIX}{timestamp}-{random.randint(100, 999)}"
            )
            output_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise BadRequestError(f"创建输出目录失败：{output_dir}（{exc}）") from exc

        params = payload.params.model_dump()

        try:
            with transaction(self.db):
                job = BackgroundJob(
                    status=BackgroundJobStatus.PENDING,
                    input_path=payload.input_path,
                    output_dir=str(output_dir),
                    files=list(payload.files or []),
                    background_path=str(background),
                    params=params,
                    source_crawl_job_id=payload.source_crawl_job_id,
                    source_crawl_note_id=payload.source_crawl_note_id,
                    total_images=len(images),
                )
                self.db.add(job)
                self.db.flush()

                allocation = allocate_output_paths(images, output_dir)

                for index, image in enumerate(images, start=1):
                    self.db.add(
                        BackgroundJobItem(
                            job_id=job.id,
                            index=index,
                            source_path=str(image),
                            source_name=image.name,
                            output_path=str(allocation[image]),
                            status=BackgroundJobItemStatus.PENDING,
                        )
                    )
                self.db.flush()

            logger.info(
                "换背景任务创建成功 | id=%s | 图片数=%s | 背景=%s | 输出=%s",
                job.id,
                len(images),
                background.name,
                output_dir,
            )
            return job
        except (BadRequestError, NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("换背景任务创建失败 | input=%s", payload.input_path)
            raise DatabaseError("换背景任务创建失败") from exc

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------

    def cancel_job(self, job_id: int) -> BackgroundJob:
        """取消任务。

        pending 任务：直接置 cancelled，工作线程认领时会跳过；排队中的条目一并
        标成 skipped（见下面的注释）；
        running 任务：置 cancelled 后由执行线程在**当前这张算完之后**发现 ——
        抠图是纯 numpy 运算，算到一半没法打断（没有子进程可杀），所以取消不是
        即时的，延迟上界 = 单张图的处理时间。这一点在页面上如实告知用户，
        见 services/background_runner.py 的模块说明。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务已处于终态。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)

                if job.status in BackgroundJobStatus.TERMINAL:
                    raise ConflictError(f"任务已是终态（{job.status}），无法取消")

                if job.status == BackgroundJobStatus.PENDING:
                    # 还没被工作线程认领（认领要求 status == pending），条目全在排队里。
                    # 一并标成「已跳过」，与 recover_interrupted_jobs 对未执行条目的
                    # 处理一致：否则详情页会出现「任务已取消，里面却躺着一堆等待中」
                    # 的自相矛盾；更要紧的是这些条目再也不会被认领，failed /
                    # skipped 计数恒为 0，用户连「重试」入口都看不到 ——
                    # 重试的候选正是 failed | skipped。
                    for item in job.items:
                        if item.status == BackgroundJobItemStatus.PENDING:
                            item.status = BackgroundJobItemStatus.SKIPPED
                            item.error_message = "任务已取消，未执行"
                            item.finished_at = utcnow()
                    # 这个任务不会再被认领，_finalize 也就永远不会跑，计数只能在这儿写
                    job.skipped_images = sum(
                        1
                        for item in job.items
                        if item.status == BackgroundJobItemStatus.SKIPPED
                    )

                job.status = BackgroundJobStatus.CANCELLED
                job.finished_at = utcnow()
                self.db.flush()

            logger.info("换背景任务已取消 | id=%s", job_id)
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("取消换背景任务失败 | id=%s", job_id)
            raise DatabaseError("取消换背景任务失败") from exc

    def update_remark(self, job_id: int, payload: JobRemarkUpdate) -> BackgroundJob:
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

            logger.info("换背景任务备注已更新 | id=%s", job_id)
            return job
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("更新换背景任务备注失败 | id=%s", job_id)
            raise DatabaseError("更新任务备注失败") from exc

    def delete_job(self, job_id: int, *, purge_files: bool = False) -> None:
        """删除任务记录（级联删除所有条目）。

        仅允许删除终态任务。默认只删记录；purge_files=True 时把任务独占的
        `background-<时间戳>/` 目录整棵删掉（释放空间是用户明确勾选的操作）。
        产物清理在记录提交之后 best-effort 执行，个别文件被占用不阻断删除。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务尚未结束。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)

                if job.status not in BackgroundJobStatus.TERMINAL:
                    raise ConflictError(f"任务尚未结束（{job.status}），请先取消后再删除")

                products = _product_paths(job) if purge_files else []
                self.db.delete(job)

            logger.info("换背景任务已删除 | id=%s | 清产物=%s", job_id, purge_files)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("删除换背景任务失败 | id=%s", job_id)
            raise DatabaseError("删除换背景任务失败") from exc

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
                    if job.status not in BackgroundJobStatus.TERMINAL:
                        raise ConflictError(
                            f"任务 #{job_id} 尚未结束（{job.status}），请先取消后再删除"
                        )
                    if purge_files:
                        products.extend(_product_paths(job))
                    self.db.delete(job)

            logger.info("换背景任务批量删除 | ids=%s | 清产物=%s", job_ids, purge_files)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("批量删除换背景任务失败 | ids=%s", job_ids)
            raise DatabaseError("批量删除任务失败") from exc

        _purge_products(products, job_ids=job_ids)
        return job_ids

    # ------------------------------------------------------------------
    # 重试
    # ------------------------------------------------------------------

    def retry_item(self, job_id: int, item_index: int) -> BackgroundJob:
        """重试单张：条目重置回 pending，任务重新入队。

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
                "换背景条目已重试入队 | job=%s | item=%s | %s",
                job_id, item_index, item.source_name,
            )
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("重试换背景条目失败 | job=%s | item=%s", job_id, item_index)
            raise DatabaseError("重试换背景条目失败") from exc

    def retry_items(self, job_id: int) -> BackgroundJob:
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
                    if entry.status in BackgroundJobItemStatus.RETRYABLE
                ]
                if not pending:
                    raise ConflictError(
                        f"任务 #{job_id} 没有可重试的条目（{job.failed_images} 条失败、"
                        f"{job.skipped_images} 条跳过）"
                    )

                self._reset_items_for_retry(job, pending)
                self.db.flush()

            logger.info(
                "换背景任务批量重试入队 | job=%s | 条目数=%s", job_id, len(pending)
            )
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("批量重试换背景任务失败 | job=%s", job_id)
            raise DatabaseError("批量重试换背景任务失败") from exc

    def _ensure_terminal_for_retry(self, job: BackgroundJob) -> None:
        """重试的公共前置：任务必须已经结束。

        Raises:
            ConflictError: 任务还在排队 / 正在跑。
        """
        if job.status not in BackgroundJobStatus.TERMINAL:
            raise ConflictError(f"任务尚未结束（{job.status}），无法重试")

    def _find_item_or_404(self, job: BackgroundJob, item_index: int) -> BackgroundJobItem:
        """按任务内序号找条目，找不到就 404。

        Raises:
            NotFoundError: 该序号不在任务里。
        """
        item: Optional[BackgroundJobItem] = next(
            (entry for entry in job.items if entry.index == item_index), None
        )
        if item is None:
            raise NotFoundError(f"任务条目不存在：job={job.id}, index={item_index}")
        return item

    @staticmethod
    def _ensure_item_retryable(item: BackgroundJobItem) -> None:
        """只有「没产出」的条目能重试：failed（跑了但失败）与 skipped（没轮到）。

        Raises:
            ConflictError: 条目是 pending / running / success。
        """
        if item.status not in BackgroundJobItemStatus.RETRYABLE:
            raise ConflictError(
                f"只有失败或跳过的条目才能重试（当前状态：{item.status}）"
            )

    def _reset_items_for_retry(
        self, job: BackgroundJob, items: List[BackgroundJobItem]
    ) -> None:
        """把给定条目重置回 pending，并把任务重新入队（单条 / 批量共用）。

        两个容易踩的地方：

        1. **计数要重算，不能清零了事**。completed_images 是执行器里 `+= 1`
           累加出来的，收尾的 _finalize 只重算 failed / skipped、不重算它，
           所以这里必须按条目状态数一遍（口径与 scene_job_service.retry_item
           一致）。之后重跑的条目会在执行时各自 +1，收尾时 failed / skipped
           再由 _finalize 数回来，总数自洽。
        2. **上一轮的残片要清掉**。产物路径按条目序号定死（output_path 连名字
           都分配好了），留着旧文件会让「产物存在」的判据失真。
        """
        for item in items:
            # 正常情况下 failed / skipped 的条目本来就没有产物，这里是防御性清理：
            # 产物是原子替换写出去的，但半截文件、手工放进来的同名图都可能存在。
            stale = Path(item.output_path)
            try:
                if stale.is_file():
                    stale.unlink()
            except OSError as exc:
                logger.warning(
                    "清理重试前的残留产物失败，跳过该文件 | %s | %s", stale, exc
                )

            item.status = BackgroundJobItemStatus.PENDING
            item.stats = {}
            item.width = 0
            item.height = 0
            item.size_bytes = 0
            item.elapsed_seconds = 0
            item.error_message = ""
            item.started_at = None
            item.finished_at = None

        job.status = BackgroundJobStatus.PENDING
        job.error_message = ""
        job.started_at = None
        job.finished_at = None
        # 换背景没有 child_pid 列（抠图跑在 worker 自己的线程里，没有子进程可杀）
        job.current_index = 0
        job.current_image = ""
        job.current_elapsed_seconds = 0
        job.completed_images = sum(
            1
            for entry in job.items
            if entry.status == BackgroundJobItemStatus.SUCCESS
        )
        job.failed_images = 0
        job.skipped_images = 0

    # ------------------------------------------------------------------
    # 产物
    # ------------------------------------------------------------------

    def get_output_path(self, job_id: int, index: int) -> Tuple[Path, str, str]:
        """按条目序号定位一张产物 PNG（供产物接口使用）。

        路径完全由任务记录推导，不接受任何外部传入的路径片段 —— 放开让前端
        传路径就是任意文件读取漏洞。序号是条目在任务内的 index（从 1 开始），
        越界与「文件还没产出」都退回 404。

        Returns:
            (文件路径, 产物文件名, 来源原图名)

        Raises:
            NotFoundError: 任务不存在、序号越界、或该张还没产出/已被移走。
        """
        job = self.get_job(job_id)
        item: Optional[BackgroundJobItem] = next(
            (entry for entry in job.items if entry.index == index), None
        )
        if item is None:
            raise NotFoundError(
                f"产物不存在：job={job_id}, index={index}（共 {job.total_images} 张）"
            )
        path = Path(item.output_path)
        if not path.is_file():
            raise NotFoundError(f"产物还没有生成，或已被移动：{item.source_name}")
        return path, path.name, item.source_name
