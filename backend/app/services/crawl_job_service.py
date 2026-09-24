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

重试：本模块**没有条目级状态**（一条任务就是一个 MC 子进程），所以「重试」只有
整任务重跑一种形态，而且是**新建一条任务**（见 retry_job）。

删除：delete_job / delete_jobs 除了删任务行，还会在同一事务里连带删掉这些
笔记已生成的 AI 文案（crawl_note_copy_service.delete_copies_for_jobs ——
crawl_note_ai_copies 表没建外键，级联只能靠 service 层）。
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
from app.services import crawl_comments, crawl_results
from app.services.crawl_note_copy_service import delete_copies_for_jobs
from app.services.crawl_runner import is_our_child
from app.services.crawler_env import probe_environment
from app.services.fs_cleanup import remove_paths_best_effort
from app.services.media_tools import terminate_process_group

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


def _collect_derived_jobs(db: Session, job_ids: List[int]) -> List[CrawlJob]:
    """找出这些任务的派生任务（评论补抓），**先同步杀掉还在跑的进程**再返回行对象。

    ⚠️ 调用点必须在 `with transaction(db):` 块内、`db.delete(...)` 之前 ——
    任务删不掉时派生任务也不该被误删。

    为什么非杀进程不可（只改状态在这里没用）：

    - runner 判断「被取消」是**轮询数据库里的状态**（`_is_cancelled` 每 tick 读一次
      crawl_jobs.status），而行马上就要被删掉，读不到就恒为 False；
    - 收尾的 `_finalize` 里 `db.get` 拿到 None 会直接 return，也不会替我们收拾。

    两条路同时断掉，MC 子进程和它拉起的 CDP Chrome 就成了没人管的孤儿：它会
    继续跑到自己结束，还在往**马上要被清掉的输出目录**里写数据。

    杀了之后 `_wait_for_exit` 下一 tick 就会发现子进程已退出、走到 `_finalize`
    的 None 分支安静收场，所以这里不必再管状态字段。

    Returns:
        派生任务行，交给调用方 db.delete 与（可选）清产物。
    """
    if not job_ids:
        return []
    jobs = list(
        db.execute(
            select(CrawlJob).where(CrawlJob.source_crawl_job_id.in_(job_ids))
        ).scalars().all()
    )
    for job in jobs:
        if job.status in CrawlJobStatus.TERMINAL or not job.child_pid:
            continue
        if is_our_child(job.child_pid):
            logger.warning(
                "删除任务，终止其派生任务的抓取进程 | job=%s | pid=%s",
                job.id, job.child_pid,
            )
            terminate_process_group(job.child_pid, settings.CRAWL_JOB_STOP_GRACE_SECONDS)
        job.child_pid = None
    return jobs


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

        **派生任务（补抓）不在这里出现**：它是用户在评论弹窗里对某一条笔记的
        补抓动作，不是一次独立的抓取，混进历史列表只会把真正想要的那些任务
        淹掉。要查它的状态走评论接口，那边返回的 refetch 字段就是它。

        Returns:
            (当前页数据, 满足条件的总条数)
        """
        try:
            # ⚠️ 条件必须加进 conditions 列表、由下面的循环同时喂给 count_stmt
            # 与 list_stmt：这两条是**独立的**语句，只给 list_stmt 加过滤会让
            # 总数虚高（total 算上了派生任务、items 里却没有），前端翻到
            # 最后一页就是一片空页。
            conditions = [CrawlJob.source_crawl_job_id.is_(None)]
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

    def create_job(
        self,
        payload: CrawlJobCreate,
        *,
        source: Optional[Tuple[int, str]] = None,
    ) -> CrawlJob:
        """创建素材抓取任务：校验、定输出目录、落库排队。

        Args:
            payload: 创建参数。
            source: 派生来源 `(来源任务 id, 笔记 id)`；补抓评论走这里，
                普通任务为 None。派生任务不进历史列表（见 list_jobs），
                删来源任务时会被级联删掉（见 delete_job）。

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
                    source_crawl_job_id=source[0] if source else None,
                    source_crawl_note_id=source[1] if source else "",
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
    # 重试
    # ------------------------------------------------------------------

    def retry_job(self, job_id: int) -> CrawlJob:
        """整任务重跑：拿旧任务的参数**新建一条任务**并返回它。

        为什么是「新建」而不是「就地重跑」：

        1. 输出目录按任务 id 定死（`<materials>/crawl/job_<id>`），就地重跑要么
           覆盖上一轮的产物、要么多出一套目录归属说不清的任务记录；
        2. MediaCrawler 的 jsonl 是 **append** 语义、`count_notes` 按行数统计且
           不去重（services/crawl_results.py）—— 就地重跑会让
           note_count / crawled_count 虚高一倍，用户看到的数字直接失真。

        为什么必须在服务端做：`login_cookies` 只存在于数据库行里，任何响应
        schema 都不会带上它（cookie 是登录凭据）—— 前端重建不出「cookie 登录」
        的任务，只有后端拿得到。

        代价（与新建任务完全一致，不额外说明）：会重新 `probe_environment()`
        并跑一遍创建时的全部校验，MC 被挪走 / 知乎补丁没打时**点击即 400** ——
        这正是想要的「失败要快」。

        刻意**沿用 create_job 的钳制**（`min(max_notes, CRAWL_MAX_NOTES_LIMIT)`、
        并发同理）：今天的环境上限说了算，而不是把旧任务里可能已经越界的数字
        原样搬过来重跑一遍。代价是管理员调低过上限时，重试的抓取量会跟着变小 ——
        新任务行的 expected_count 会如实反映这一点。

        Raises:
            NotFoundError: 任务不存在。
            BadRequestError: 旧任务的参数按今天的规则不再合法（工具没了、补丁没了、
                参数越界等）—— 由 create_job 抛出。
            DatabaseError: 写库失败（事务已回滚）。
        """
        source = self.get_job(job_id)
        params = dict(source.params or {})

        # 只挑 CrawlJobCreate 认的键：params 是 JSON 列，历史任务里可能留着
        # 已经改名的旧键，多传一个未知键会直接 422。
        payload = CrawlJobCreate(
            platform=source.platform,
            crawler_type=source.crawler_type,
            login_type=source.login_type,
            cookies=source.login_cookies or "",
            keywords=params.get("keywords"),
            ids=params.get("ids"),
            creators=params.get("creators"),
            start_page=params.get("start_page", 1),
            max_notes=params.get("max_notes", 20),
            get_comments=params.get("get_comments", True),
            get_sub_comments=params.get("get_sub_comments", False),
            max_comments=params.get("max_comments", 10),
            headless=params.get("headless", False),
            max_concurrency=params.get("max_concurrency", 1),
        )

        job = self.create_job(payload)
        logger.info("素材抓取任务重试（新建） | 原任务=%s | 新任务=%s", job_id, job.id)
        return job

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
                # 先杀派生任务的进程、再删行（顺序不能反，见 _collect_derived_jobs）
                derived = _collect_derived_jobs(self.db, [job_id])
                if purge_files:
                    for child in derived:
                        products.extend(_product_paths(child))
                # 同事务连带删掉这些笔记已生成的 AI 文案（表上没建外键，级联靠这里）
                delete_copies_for_jobs(self.db, [job_id, *(child.id for child in derived)])
                for child in derived:
                    self.db.delete(child)
                self.db.delete(job)

            logger.info(
                "素材抓取任务已删除 | id=%s | 清产物=%s | 连带派生任务=%s 条",
                job_id, purge_files, len(derived),
            )
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
                # 派生任务也一并删（先杀进程再删行，见 _collect_derived_jobs）
                derived = _collect_derived_jobs(self.db, job_ids)
                if purge_files:
                    for child in derived:
                        products.extend(_product_paths(child))
                for child in derived:
                    self.db.delete(child)
                # 同事务连带删掉这些笔记已生成的 AI 文案（一条 SQL 删整批）
                delete_copies_for_jobs(self.db, [*job_ids, *(child.id for child in derived)])

            logger.info(
                "素材抓取任务批量删除 | ids=%s | 清产物=%s | 连带派生任务=%s 条",
                job_ids, purge_files, len(derived),
            )
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

    # ------------------------------------------------------------------
    # 评论与补抓
    # ------------------------------------------------------------------

    def get_note_comments(self, job_id: int, note_id: str) -> dict:
        """读一条笔记的评论：评论树 + 原任务的评论配置 + 最新一次补抓的状态。

        **读取口径是「根任务的产物 + 它全部补抓任务的产物」**：评论可能抓过
        好几次，散在各自的输出目录里，要合起来才是用户心里那个「这条笔记的
        评论」。传进来的 job_id 是派生任务时先解析回根任务 —— 前端手上拿着
        哪个 id 都该得到同一个答案。

        Raises:
            NotFoundError: 任务不存在，或这条笔记不在任务结果里。
            DatabaseError: 查库失败。
        """
        try:
            root = self._resolve_root_job(self.get_job(job_id))
            notes = crawl_results.collect_results(Path(root.output_dir), root.platform)
            note = crawl_results.find_note(notes, note_id)
            if note is None:
                raise NotFoundError(
                    "这条笔记不在任务结果里（结果可能已变化，请刷新结果列表）"
                )

            derived = self._list_derived_jobs(root.id, note_id)
            output_dirs = [Path(root.output_dir)]
            for child in derived:
                if child.output_dir:
                    output_dirs.append(Path(child.output_dir))

            comments = crawl_comments.collect_comments(
                output_dirs, root.platform, note_id
            )
            # 评论图是带时效签名的 URL，必须本地化缓存（缓存放根任务目录，
            # 删任务时随目录清掉）；下不到的保持 URL，见 localize_pictures 注释
            crawl_comments.localize_pictures(
                output_dirs[0], root.platform, comments
            )
            # 按 id 升序取的，最后一条就是最近一次补抓
            latest = derived[-1] if derived else None
            return {
                "job_id": root.id,
                "note_id": note_id,
                "platform": root.platform,
                "comments": comments,
                "total": crawl_comments.count_comments(comments),
                "top_level_total": len(comments),
                "comments_config": _comments_config(root.params or {}),
                # 补抓设置里登录方式 / 无头的默认选中项（用户可改选，见补抓接口）
                "login_type": root.login_type,
                "headless": bool((root.params or {}).get("headless")),
                "refetch": self._refetch_state(latest, note_id) if latest else None,
            }
        except (NotFoundError, BadRequestError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("读取笔记评论失败 | job=%s | note=%s", job_id, note_id)
            raise DatabaseError("读取笔记评论失败") from exc

    def create_comment_refetch(
        self,
        job_id: int,
        note_id: str,
        *,
        max_comments: int,
        sub_comments: bool,
        login_type: Optional[str] = None,
        cookies: Optional[str] = None,
        headless: Optional[bool] = None,
    ) -> CrawlJob:
        """对一条笔记补抓评论：建一条派生任务（detail 模式，只抓这一条）。

        三个刻意的选择：

        - **不复用原任务的 max_comments**：触发补抓的典型场景恰恰是原任务
          `max_comments=0` 或压根没开评论开关，继承它等于抓 0 条还报 success。
          条数由用户在弹窗里现选。
        - **登录方式 / 无头默认继承原任务，但允许覆盖**：cookie 登录最常见的
          败因是串已过期，补抓正是用户换登录方式（改扫码、或贴新 cookie）的
          天然时机。沿用旧 cookie 只能由服务端做 —— 它只存在于数据库行里，
          任何响应 schema 都不带它。选了 cookie 但没贴新串时回退到原任务存的；
          原任务是扫码、这边又改选 cookie 且没贴串的，交给 create_job 的
          校验报 400，不排一个注定失败的任务。
        - **复用 create_job**：MC 装没装、参数钳制、输出目录命名这一整套校验
          只该有一份实现。

        Raises:
            NotFoundError: 任务不存在，或这条笔记不在任务结果里。
            BadRequestError: 这条笔记补抓不了（原因见 crawl_comments），或选了
                cookie 登录却没有可用的 cookie 串。
            ConflictError: 这条笔记已经有一个还没结束的补抓任务（details 带它的 id）。
            DatabaseError: 写库失败（事务已回滚）。
        """
        root = self._resolve_root_job(self.get_job(job_id))
        notes = crawl_results.collect_results(Path(root.output_dir), root.platform)
        note = crawl_results.find_note(notes, note_id)
        if note is None:
            raise NotFoundError(
                "这条笔记不在任务结果里（结果可能已变化，请刷新结果列表）"
            )

        # 能不能补抓、喂什么形态，逐平台的规则在 crawl_comments 里（纯函数）
        spec = crawl_comments.refetch_spec(root.platform, note)
        if spec is None:
            raise BadRequestError(
                crawl_comments.refetch_unsupported_reason(root.platform, note)
            )

        self._ensure_no_live_refetch(root.id, note_id)

        params = root.params or {}
        effective_login = login_type or root.login_type
        if effective_login == CrawlLoginType.QRCODE:
            # 扫码登录与 cookie 串互斥：换扫码时把旧串清掉，别带进新任务
            effective_cookies = ""
        else:
            # 没贴新串（None / 空白）就沿用原任务存的 —— cookie 凭据不出库，
            # 前端拿到的输入框永远是空的，「留空 = 沿用」是唯一说得通的语义
            effective_cookies = (cookies or "").strip() or root.login_cookies or ""
        payload = CrawlJobCreate(
            platform=root.platform,
            crawler_type=CrawlerType.DETAIL,
            login_type=effective_login,
            cookies=effective_cookies,
            ids=[spec],
            max_notes=1,
            get_comments=True,
            get_sub_comments=sub_comments,
            max_comments=max_comments,
            headless=headless if headless is not None else bool(params.get("headless")),
            max_concurrency=_to_int(params.get("max_concurrency")) or 1,
        )
        job = self.create_job(payload, source=(root.id, note_id))
        logger.info(
            "评论补抓任务已创建 | 原任务=%s | 新任务=%s | 平台=%s | 笔记=%s",
            root.id, job.id, root.platform, note_id,
        )
        return job

    def _resolve_root_job(self, job: CrawlJob) -> CrawlJob:
        """把派生任务解析回它的根任务。

        做成循环而不是解一层：根任务自己理论上不可能是派生的（补抓只从根任务
        发起），但历史数据异常时不至于死循环 —— 循环里有 seen 兜底。
        """
        seen = set()
        current = job
        while current.source_crawl_job_id and current.id not in seen:
            seen.add(current.id)
            parent = self.db.get(CrawlJob, current.source_crawl_job_id)
            if parent is None:
                break
            current = parent
        return current

    def _list_derived_jobs(self, root_id: int, note_id: str) -> List[CrawlJob]:
        """列出某条笔记的全部补抓任务，按 id 升序（= 先后顺序）。"""
        stmt = (
            select(CrawlJob)
            .where(
                CrawlJob.source_crawl_job_id == root_id,
                CrawlJob.source_crawl_note_id == note_id,
            )
            .order_by(CrawlJob.id.asc())
        )
        return list(self.db.execute(stmt).scalars().all())

    def _ensure_no_live_refetch(self, root_id: int, note_id: str) -> None:
        """同一条笔记同时只允许有一个还没结束的补抓任务。

        挡的是双击、多标签页这类重复提交：MC 是单任务串行，重复的任务会白跑
        一遍还排在队列后面。带上已存在那条的 job_id，前端可以直接切到
        「正在补抓」的状态。

        残余竞态：两个请求同时通过检查时会各建一条（表上没有能表达「同一笔记
        只允许一个未结束任务」的约束 —— 部分唯一索引管不住状态流转）。后果只是
        白跑一次抓取，不会写坏数据，所以不为此加锁。

        Raises:
            ConflictError: 已有未结束的补抓任务。
        """
        stmt = (
            select(CrawlJob)
            .where(
                CrawlJob.source_crawl_job_id == root_id,
                CrawlJob.source_crawl_note_id == note_id,
                CrawlJob.status.notin_(CrawlJobStatus.TERMINAL),
            )
            .order_by(CrawlJob.id.asc())
            .limit(1)
        )
        existing = self.db.execute(stmt).scalars().first()
        if existing is not None:
            raise ConflictError(
                f"这条笔记已经有一个补抓任务还没结束（#{existing.id}），"
                "等它跑完或先取消它",
                details={"job_id": existing.id},
            )

    def _refetch_state(self, job: CrawlJob, note_id: str) -> dict:
        """把一条补抓任务转成前端要的状态块。"""
        # 这次补抓自己抓到几条评论：**必须单独数**，不能拿 job.note_count 顶 ——
        # 判 success 用的是 contents 行数（补抓只有 1 条内容），跟评论数无关。
        comment_count = 0
        if job.output_dir:
            comment_count = len(
                crawl_comments.scan_comments(Path(job.output_dir), job.platform, note_id)
            )
        return {
            "job_id": job.id,
            "status": job.status,
            "error_message": refetch_failure_hint(job.error_message),
            "comment_count": comment_count,
            "queued_ahead": self._queued_ahead(job),
        }

    def _queued_ahead(self, job: CrawlJob) -> int:
        """排在它前面的待执行任务数。

        MC 是单任务串行、`claim_next_pending_id` 按 id 升序 FIFO。补抓任务被
        挡在历史列表之外，用户在界面上看不到队列 —— 没有这个数字，面对一个
        转二十分钟的「正在补抓」完全无法理解发生了什么。
        """
        if job.status != CrawlJobStatus.PENDING:
            return 0
        stmt = (
            select(func.count())
            .select_from(CrawlJob)
            .where(
                CrawlJob.status == CrawlJobStatus.PENDING,
                CrawlJob.id < job.id,
            )
        )
        return int(self.db.execute(stmt).scalar_one())


#: 补抓失败的日志指纹 → 一句能看懂的中文原因。MC 的原始报错全是英文日志行
#: （一屏十几行），用户从里面读不出「我该做什么」。这几条是实测撞到的形态，
#: 认不出来就原样返回，宁可啰嗦也别瞎解释。
_REFETCH_FAILURE_HINTS: tuple = (
    (
        # 必须排在 "Failed to get note detail" 前面：解析失败是**另一回事**
        # （页面拿到了，是抓取器读不懂），两个指纹可能同一份日志里都有，
        # 顺序反了就会给出「笔记可能已删除」这种指错方向的解释。
        "Failed to parse note detail html",
        "小红书改了笔记页面的数据格式，抓取器读不懂页面里返回的内容"
        "（不是登录态或链接失效，换扫码、重试都不会变好）。"
        "出错位置与上下文记在下面的日志里，可用于排查。",
    ),
    (
        "Failed to get note detail",
        "小红书没有返回这条笔记的详情（笔记可能已被删除，或访问令牌 / 登录态已失效）。"
        "点「再抓一次」，在补抓设置里把登录方式换成扫码试试；或重新登录小红书后再补抓。",
    ),
    (
        "Login state result: False",
        "小红书登录态已失效（Cookie 过期）。点「再抓一次」，把登录方式换成扫码登录即可。",
    ),
)


def refetch_failure_hint(error_message: str) -> str:
    """补抓失败的 error_message：认出已知原因时**在最前面**加一句中文解释。

    原始日志保留在后面（毕竟它才是完整的现场），但用户第一眼看到的是那句
    中文 —— 弹窗里的失败提示只给一堵英文日志墙等于什么都没说。
    """
    text = error_message or ""
    if not text:
        return text
    for marker, hint in _REFETCH_FAILURE_HINTS:
        if marker in text:
            return f"{hint}\n\n（以下为抓取日志）\n{text}"
    return text


def _comments_config(params: dict) -> dict:
    """原任务的评论采集配置（三态判定的前两态靠 enabled 区分）。

    **enabled 要两个条件同时满足**：只开了 get_comments 但 max_comments=0 时
    MC 一条都不会抓（评论文件根本不会生成），把它算成「开了」会让用户对着
    空列表反复点「再抓一次」。
    """
    max_comments = _to_int(params.get("max_comments"))
    return {
        "enabled": bool(params.get("get_comments")) and max_comments > 0,
        "max_comments": max_comments,
        "sub_comments": bool(params.get("get_sub_comments")),
    }


def _to_int(value) -> int:
    """params 是从 JSON 列里读出来的，类型不保证；转不了的按 0。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
