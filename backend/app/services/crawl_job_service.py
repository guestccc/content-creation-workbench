"""素材抓取任务的业务逻辑层。

职责边界（与 subtitle_job_service.py 一致）：
- 本模块只管「校验 + 状态机 + 查询」，完全不碰子进程；
- 真正起 MediaCrawler 子进程的执行逻辑在 crawl_runner.py，
  工作线程在 crawl_job_worker.py。

任务生命周期：

    pending ──工作线程认领──> running ──抓到内容──> success
       │                       │
       │                       └──没抓到任何内容──> failed
       └──取消──> cancelled（running 中取消：杀进程，已抓内容保留）

创建时的校验要点（预期内的错误在点击时就报，而不是排队后炸）：
- 工具没装 / 知乎 creator 没打补丁 / cookie 登录没给 cookie；
- 模式专属参数：search 要关键词，detail/creator 要非空列表。
"""

from pathlib import Path
from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import BadRequestError, ConflictError, DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.core.materials import CRAWL, subdir
from app.db.session import transaction
from app.models.content import utcnow
from app.models.crawl_job import (
    CrawlJob,
    CrawlJobStatus,
    CrawlLoginType,
    CrawlPlatform,
    CrawlerType,
)
from app.schemas.common import JobRemarkUpdate
from app.schemas.crawl_job import CrawlJobCreate
from app.services import crawl_results
from app.services.crawler_env import probe_environment
from app.services.fs_cleanup import remove_paths_best_effort

logger = get_logger(__name__)

#: runner 侧的日志文件名（路径在创建任务时就定死，收尾读它做失败摘要）。
MC_LOG_NAME = "mc.log"


def _product_paths(job: CrawlJob) -> List[Path]:
    """列出一条任务的产物路径（只从任务记录推导，不接受外部路径）。

    输出目录是 subdir(CRAWL)/job_<id>，任务独占、jsonl/媒体文件/mc.log
    全在里面，整棵删。
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


def estimate_expected_count(crawler_type: str, params: dict) -> int:
    """预估抓取总量（进度条的分母，只是估算）。

    search：关键词数 × 每词上限 —— 单个关键词能出多少条不可知，用上限当
    分母是「最乐观估计」，进度条因此只会偏慢不会虚满。
    detail：链接列表长度（确定的）。
    creator：创作者数 × 每人上限（同样偏乐观）。搜索页还有最小每页数
    （xhs 每页至少 20 条）、去重与风控截流，实际值完全可能更少 ——
    progress_percent 对此的处理见 schemas/crawl_job._progress_percent。
    """
    if crawler_type == CrawlerType.DETAIL:
        return len(params.get("ids") or [])
    max_notes = int(params.get("max_notes") or 0)
    if crawler_type == CrawlerType.SEARCH:
        return max_notes * max(1, len(params.get("keywords") or []))
    if crawler_type == CrawlerType.CREATOR:
        return max_notes * max(1, len(params.get("creators") or []))
    return 0


class CrawlJobService:
    """素材抓取任务服务。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _get_job_or_404(self, job_id: int) -> CrawlJob:
        """按 ID 取任务，不存在则抛 NotFoundError。"""
        job = self.db.get(CrawlJob, job_id)
        if job is None:
            raise NotFoundError(f"素材抓取任务不存在：id={job_id}")
        return job

    def get_job(self, job_id: int) -> CrawlJob:
        """按 ID 获取任务。"""
        try:
            return self._get_job_or_404(job_id)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("查询抓取任务失败 | id=%s", job_id)
            raise DatabaseError("查询抓取任务失败") from exc

    def list_jobs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> Tuple[List[CrawlJob], int]:
        """分页查询任务，按创建时间倒序（即 ID 倒序）。

        Returns:
            (当前页数据, 满足条件的总条数)
        """
        try:
            conditions = []
            if status:
                conditions.append(CrawlJob.status == status)
            if platform:
                conditions.append(CrawlJob.platform == platform)

            count_stmt = select(func.count()).select_from(CrawlJob)
            list_stmt = select(CrawlJob)
            for condition in conditions:
                count_stmt = count_stmt.where(condition)
                list_stmt = list_stmt.where(condition)

            total = self.db.execute(count_stmt).scalar_one()

            list_stmt = (
                list_stmt.order_by(CrawlJob.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            items = list(self.db.execute(list_stmt).scalars().all())
            return items, int(total)
        except SQLAlchemyError as exc:
            logger.exception("查询抓取任务列表失败")
            raise DatabaseError("查询抓取任务列表失败") from exc

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def create_job(self, payload: CrawlJobCreate) -> CrawlJob:
        """创建素材抓取任务：校验、定输出目录、落库排队。

        Raises:
            BadRequestError: 未装 MC、知乎 creator 未打补丁、模式参数缺失、
                cookie 登录没给 cookie。
            DatabaseError: 写库失败（事务已回滚）。
        """
        # 建任务前先确认工具可用：失败要快，让用户在点击时就看到原因。
        # 环境探测带缓存，这里只是读缓存 + 补两项文件检查，不贵。
        environment = probe_environment()
        if not environment["installed"]:
            raise BadRequestError(
                "未检测到可用的 MediaCrawler，请先按页面上方的指引安装后再试"
            )
        if (
            payload.platform == CrawlPlatform.ZHIHU
            and payload.crawler_type == CrawlerType.CREATOR
            and not environment["zhihu_creator_cli_supported"]
        ):
            raise BadRequestError(
                "知乎的创作者模式还需要给 MediaCrawler 打一个小补丁：在"
                " cmd_arg/arg.py 的 creator_id 分支里补上 ZHIHU_CREATOR_URL_LIST"
                "（详见项目文档），或改用搜索/详情模式"
            )

        if payload.crawler_type == CrawlerType.SEARCH and not payload.keywords:
            raise BadRequestError("搜索模式需要至少一个关键词")
        if payload.crawler_type == CrawlerType.DETAIL and not payload.ids:
            raise BadRequestError("详情模式需要至少一条笔记/作品链接")
        if payload.crawler_type == CrawlerType.CREATOR and not payload.creators:
            raise BadRequestError("创作者模式需要至少一条主页链接或 ID")

        if payload.login_type == CrawlLoginType.COOKIE and not payload.cookies.strip():
            raise BadRequestError("cookie 登录需要提供 cookie 串（从浏览器开发者工具复制）")

        params = {
            "keywords": payload.keywords or [],
            "ids": payload.ids or [],
            "creators": payload.creators or [],
            "start_page": payload.start_page,
            "max_notes": min(payload.max_notes, settings.CRAWL_MAX_NOTES_LIMIT),
            "get_comments": payload.get_comments,
            "get_sub_comments": payload.get_sub_comments,
            "max_comments": payload.max_comments,
            # 无头与扫码登录并不冲突：MC 扫码时把页面二维码抓出来用系统看图软件
            # 弹出来（tools/crawler_util.show_qrcode），不依赖浏览器窗口。
            # 真正的风险是滑块/风控验证需要可见窗口 —— 那是用户的可选项，不硬压。
            "headless": payload.headless,
            "max_concurrency": min(
                payload.max_concurrency, settings.CRAWL_MAX_CONCURRENCY_LIMIT
            ),
        }

        try:
            with transaction(self.db):
                job = CrawlJob(
                    status=CrawlJobStatus.PENDING,
                    platform=payload.platform,
                    crawler_type=payload.crawler_type,
                    login_type=payload.login_type,
                    params=params,
                    login_cookies=payload.cookies.strip(),
                    expected_count=estimate_expected_count(payload.crawler_type, params),
                )
                self.db.add(job)
                # flush 拿到 id 才能定「一任务一目录」的输出目录
                self.db.flush()
                output_dir = subdir(CRAWL) / f"job_{job.id}"
                job.output_dir = str(output_dir)
                job.log_path = str(output_dir / MC_LOG_NAME)
                self.db.flush()

            logger.info(
                "素材抓取任务创建成功 | id=%s | 平台=%s | 模式=%s | 输出=%s",
                job.id, job.platform, job.crawler_type, job.output_dir,
            )
            return job
        except (BadRequestError, NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("素材抓取任务创建失败 | 平台=%s", payload.platform)
            raise DatabaseError("素材抓取任务创建失败") from exc

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------

    def cancel_job(self, job_id: int) -> CrawlJob:
        """取消任务。

        pending 任务：直接置 cancelled，工作线程认领时会跳过；
        running 任务：置 cancelled 后由执行线程在下一次 tick（≤0.5 秒）发现，
        整组杀掉 MC 子进程（含 CDP Chrome），已抓到的 jsonl 保留。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务已处于终态。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)

                if job.status in CrawlJobStatus.TERMINAL:
                    raise ConflictError(f"任务已是终态（{job.status}），无法取消")

                job.status = CrawlJobStatus.CANCELLED
                job.finished_at = utcnow()
                self.db.flush()

            logger.info("素材抓取任务已取消 | id=%s", job_id)
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("取消抓取任务失败 | id=%s", job_id)
            raise DatabaseError("取消抓取任务失败") from exc

    def delete_job(self, job_id: int, *, purge_files: bool = False) -> None:
        """删除任务记录。

        仅允许删除终态任务。默认只删记录，输出目录里的 jsonl 与媒体文件
        保留 —— 那是用户的产物（图文二创的素材就来自这里），删记录不删文件。
        purge_files=True 时把任务输出目录整棵删掉（释放空间是用户明确勾选的
        操作）。产物清理在记录提交之后 best-effort 执行，个别文件被占用不阻断
        删除。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务尚未结束。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)

                if job.status not in CrawlJobStatus.TERMINAL:
                    raise ConflictError(f"任务尚未结束（{job.status}），请先取消后再删除")

                products = _product_paths(job) if purge_files else []
                self.db.delete(job)

            logger.info("素材抓取任务已删除 | id=%s | 清产物=%s", job_id, purge_files)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("删除抓取任务失败 | id=%s", job_id)
            raise DatabaseError("删除抓取任务失败") from exc

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
                    if job.status not in CrawlJobStatus.TERMINAL:
                        raise ConflictError(
                            f"任务 #{job_id} 尚未结束（{job.status}），请先取消后再删除"
                        )
                    if purge_files:
                        products.extend(_product_paths(job))
                    self.db.delete(job)

            logger.info("素材抓取任务批量删除 | ids=%s | 清产物=%s", job_ids, purge_files)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("批量删除抓取任务失败 | ids=%s", job_ids)
            raise DatabaseError("批量删除任务失败") from exc

        _purge_products(products, job_ids=job_ids)
        return job_ids

    # ------------------------------------------------------------------
    # 编辑
    # ------------------------------------------------------------------

    def update_remark(self, job_id: int, payload: JobRemarkUpdate) -> CrawlJob:
        """更新任务备注（空串表示清空）。

        备注是纯用户标记，不参与状态机，任何状态下都允许改。

        Raises:
            NotFoundError: 任务不存在。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self.db.get(CrawlJob, job_id)
                if job is None:
                    raise NotFoundError(f"素材抓取任务不存在：id={job_id}")
                job.remark = payload.remark
                self.db.flush()

            logger.info("素材抓取任务备注已更新 | id=%s", job_id)
            return job
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("更新抓取任务备注失败 | id=%s", job_id)
            raise DatabaseError("更新任务备注失败") from exc

    # ------------------------------------------------------------------
    # 产物
    # ------------------------------------------------------------------

    def get_results(self, job_id: int) -> List[dict]:
        """读取任务的归一化笔记列表（终态后是定稿，running 中是当前已抓到的）。

        Raises:
            NotFoundError: 任务不存在。
        """
        job = self.get_job(job_id)
        return crawl_results.collect_results(Path(job.output_dir), job.platform)

    def get_log_tail(self, job_id: int, limit: int = 4000) -> str:
        """MC 子进程日志尾部（失败原因展示用）。

        Raises:
            NotFoundError: 任务不存在。
        """
        from app.services.media_tools import read_log_tail

        job = self.get_job(job_id)
        return read_log_tail(Path(job.log_path), limit=limit)

    def get_job_phase(self, job: CrawlJob) -> str:
        """从日志尾部推断 running 任务的当前阶段（非 running 返回空串）。"""
        if job.status != CrawlJobStatus.RUNNING:
            return ""
        from app.services.crawl_runner import detect_phase
        from app.services.media_tools import read_log_tail

        tail = read_log_tail(Path(job.log_path), limit=4000)
        return detect_phase(tail, login_type=job.login_type, crawled_count=job.crawled_count)

    def resolve_media_path(self, job_id: int, relative: str) -> Path:
        """把前端请求的相对路径解析成任务输出目录内的文件绝对路径。

        Raises:
            NotFoundError: 任务不存在或路径越界 / 文件不存在。
        """
        job = self.get_job(job_id)
        target = crawl_results.media_file(Path(job.output_dir), relative)
        if target is None:
            raise NotFoundError(f"媒体文件不存在：{relative}")
        return target
