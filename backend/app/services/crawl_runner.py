"""素材抓取任务的执行层：起 MediaCrawler 子进程、盯进度、收尾。

与 subtitle_runner.py 完全同构的骨架（子进程三条硬规则原样照抄），差别
只在被调工具的形态：

- 一个任务 = 一个 MC 子进程（关键词/链接列表都在一次 CLI 调用里传），
  没有 items 循环；
- cwd 必须是 MC 仓库根（它的 config/browser_data 都按仓库相对位置解析）；
- **进度 = 已产出的 jsonl 行数**：MC 的日志是给人看的 rich 输出，翻页
  格式不规整，解析它等于给上游的日志格式写依赖。jsonl 是它的正式产物，
  行数是我们自己写的解析（crawl_results.count_notes），可靠；
- 判成功**以产物为准**：MC 会在收尾阶段（关浏览器、清理）报错而数据其实
  已落盘；反过来退出码 0 也可能一条都没抓到（风控拦截时不一定抛异常）。
  所以「抓到了 N 条」是硬指标，退出码只用来写备注。
- 整组 kill 会连 MC 拉起的 CDP Chrome 一起杀掉（media_tools 在 Windows
  用 taskkill /T）—— 这是预期行为，CDP 用的是独立 profile，不影响用户
  自己开着的浏览器。

MediaCrawler 的 LICENSE 是非商用学习协议：本功能仅供个人学习研究自用。
"""

import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal, transaction
from app.models.content import utcnow
from app.models.crawl_job import (
    CrawlJob,
    CrawlJobStatus,
)
from app.services import crawl_results
from app.services.crawler_env import detect
from app.services.media_tools import (
    CHILD_CREATION_FLAGS,
    child_env,
    command_line,
    read_log_tail,
    terminate_process_group,
)

logger = get_logger(__name__)

#: 子进程日志名：与任务输出同目录，失败时读它的尾部给用户看真实报错。
MC_LOG_NAME = "mc.log"

#: 抓取参数的**白名单键**。params 来自数据库，不信任其中的额外内容：
#: 只认这些，其余一律忽略（build_argv 里逐项取值）。
ALLOWED_PARAMS = (
    "keywords",
    "ids",
    "creators",
    "start_page",
    "max_notes",
    "get_comments",
    "get_sub_comments",
    "max_comments",
    "headless",
    "max_concurrency",
)

#: 失败摘要里优先挑出来的行。MC 用 loguru 记日志，错误行带 ERROR 级别
#: 标记；❌ 和 Traceback 兜底（平台风控提示常是普通 info 行带 ❌）。
_ERROR_MARKERS = ("ERROR", "Error", "❌", "Traceback")


def build_argv(
    launcher: List[str],
    *,
    platform: str,
    crawler_type: str,
    login_type: str,
    params: dict,
    output_dir: str,
    cookies: str = "",
) -> List[str]:
    """把任务参数映射成 MediaCrawler main.py 的 argv（纯函数，可单测）。

    细节（读 MC 的 cmd_arg/arg.py 确认）：
    - bool 参数（--get_comment 等）声明为 str 再 str2bool，统一传
      "true"/"false"；
    - 列表参数（--keywords / --specified_id / --creator_id）是逗号分隔的
      单参数 —— 值里的英文逗号会被当分隔符，这是 MC 的约定，无解；
    - 走 list argv 不经过 shell，值里的空格/引号没有注入面。
    """
    argv = list(launcher) + [
        "main.py",
        "--platform", str(platform),
        "--type", str(crawler_type),
        "--lt", str(login_type),
        "--start", str(int(params.get("start_page") or 1)),
        "--crawler_max_notes_count", str(int(params.get("max_notes") or 20)),
        "--max_comments_count_singlenotes", str(int(params.get("max_comments") or 0)),
        "--max_concurrency_num", str(int(params.get("max_concurrency") or 1)),
        "--get_comment", "true" if params.get("get_comments") else "false",
        "--get_sub_comment", "true" if params.get("get_sub_comments") else "false",
        "--headless", "true" if params.get("headless") else "false",
        "--save_data_option", "jsonl",
        "--save_data_path", str(output_dir),
    ]

    if crawler_type == "search":
        keywords = [str(k) for k in (params.get("keywords") or []) if str(k).strip()]
        argv += ["--keywords", ",".join(keywords)]
    elif crawler_type == "detail":
        ids = [str(i) for i in (params.get("ids") or []) if str(i).strip()]
        argv += ["--specified_id", ",".join(ids)]
    elif crawler_type == "creator":
        creators = [str(c) for c in (params.get("creators") or []) if str(c).strip()]
        argv += ["--creator_id", ",".join(creators)]

    if login_type == "cookie" and cookies:
        argv += ["--cookies", cookies]

    return argv


def is_our_child(pid: int) -> bool:
    """校验一个 PID 现在确实还是我们起的 MediaCrawler 进程。

    机器重启后 PID 会被复用，孤儿回收前必须核对命令行身份。
    Windows 上 command_line 拿不到内容（返回空串），回收只能退化为
    「直接标 failed 不杀进程」—— 与 subtitle_runner 的现有行为一致。
    """
    if pid <= 0:
        return False
    command = command_line(pid)
    if not command:
        return False
    return "main.py" in command and "MediaCrawler" in command


def summarize_failure(text: str) -> str:
    """从 MC 日志里挑一行能说明问题的文字作为失败摘要。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for marker in _ERROR_MARKERS:
        for line in lines:
            if marker in line:
                return line
    return lines[-1] if lines else ""


def probe_environment(*, refresh: bool = False) -> dict:
    """探测素材抓取功能的运行环境（委托 crawler_env，保持与 subtitle 对称）。"""
    from app.services.crawler_env import probe_environment as _probe

    return _probe(refresh=refresh)


# --------------------------------------------------------------------------
# 队列认领（DB 即队列：pending 行就是待执行任务）
# --------------------------------------------------------------------------


def claim_next_pending_id(session_factory: sessionmaker = SessionLocal) -> Optional[int]:
    """取下一个待执行任务的 ID（只读不改状态，真正抢锁在 run_job 里）。"""
    with session_factory() as db:
        row = db.execute(
            select(CrawlJob.id)
            .where(CrawlJob.status == CrawlJobStatus.PENDING)
            .order_by(CrawlJob.id.asc())
            .limit(1)
        ).first()
        return int(row[0]) if row else None


def recover_interrupted_jobs(
    session_factory: sessionmaker = SessionLocal,
    *,
    stop_grace_seconds: Optional[float] = None,
) -> int:
    """启动时回收上次异常退出留下的 running 任务。

    有 child_pid 的先核对身份再整组杀掉（防 PID 复用误杀；Windows 上核对
    不了，只标状态不杀）；任务标记 failed —— 已抓到的 jsonl 还在输出目录，
    用户仍可查看部分结果。pending 任务不动，DB 队列会自动接手。
    """
    grace = (
        stop_grace_seconds
        if stop_grace_seconds is not None
        else settings.CRAWL_JOB_STOP_GRACE_SECONDS
    )
    recovered = 0

    with session_factory() as db:
        try:
            with transaction(db):
                jobs = list(
                    db.execute(
                        select(CrawlJob).where(CrawlJob.status == CrawlJobStatus.RUNNING)
                    )
                    .scalars()
                    .all()
                )
                for job in jobs:
                    if job.child_pid and is_our_child(job.child_pid):
                        logger.warning(
                            "回收残留的抓取进程组 | job=%s | pid=%s", job.id, job.child_pid
                        )
                        terminate_process_group(job.child_pid, grace)
                    job.note_count = crawl_results.count_notes(
                        Path(job.output_dir), job.platform
                    )
                    job.status = CrawlJobStatus.FAILED
                    job.error_message = "服务重启，任务中断"
                    job.finished_at = utcnow()
                    job.child_pid = None
                    recovered += 1
                db.flush()
        except Exception:  # noqa: BLE001 - 启动恢复绝不能阻断服务启动
            logger.exception("回收中断的抓取任务失败")
            return 0

    if recovered:
        logger.info("已回收中断的素材抓取任务 | 数量=%s", recovered)
    return recovered


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------


class CrawlRunner:
    """素材抓取执行器：一个任务起一个 MediaCrawler 子进程。

    所有外部依赖都可注入，测试传 FakeMcPopen + tick_seconds=0 即可在主线程
    同步跑完，不需要线程也不需要真爬虫。
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker = SessionLocal,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        launcher: Optional[List[str]] = None,
        mc_root: Optional[str] = None,
        tick_seconds: Optional[float] = None,
        progress_seconds: Optional[float] = None,
        timeout_seconds: Optional[int] = None,
        stop_grace_seconds: Optional[float] = None,
    ) -> None:
        """初始化执行器。

        Args:
            session_factory: 数据库会话工厂（执行线程自己开会话，与请求线程隔离）。
            popen_factory: 子进程工厂，测试注入替身。
            launcher: 调用前缀；默认每次执行时探测（探测有缓存，不贵）。
            mc_root: MC 仓库根（子进程 cwd）；默认取配置。
            tick_seconds: 等待子进程时的轮询间隔。
            progress_seconds: 进度字段落库的最小间隔。
            timeout_seconds: 任务整体硬超时。
            stop_grace_seconds: 杀进程组的宽限秒数。
        """
        self.session_factory = session_factory
        self.popen_factory = popen_factory
        self._launcher = launcher
        self.mc_root = mc_root if mc_root is not None else settings.CRAWL_MC_ROOT
        self.tick_seconds = (
            tick_seconds if tick_seconds is not None else settings.CRAWL_JOB_TICK_SECONDS
        )
        self.progress_seconds = (
            progress_seconds
            if progress_seconds is not None
            else settings.CRAWL_JOB_PROGRESS_SECONDS
        )
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.CRAWL_JOB_TIMEOUT_SECONDS
        )
        self.stop_grace_seconds = (
            stop_grace_seconds
            if stop_grace_seconds is not None
            else settings.CRAWL_JOB_STOP_GRACE_SECONDS
        )

    @property
    def launcher(self) -> Optional[List[str]]:
        """当前可用的调用前缀；没装 MediaCrawler 时为 None。"""
        if self._launcher is not None:
            return self._launcher
        install = detect()
        return list(install.launcher) if install.installed else None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run_job(self, job_id: int, stop_event=None) -> bool:
        """认领并执行一个任务。

        Returns:
            True 表示执行了该任务；False 表示任务不存在、已被取消或已被抢占。
        """
        with self.session_factory() as db:
            if not self._claim(db, job_id):
                return False

        with self.session_factory() as db:
            job = db.get(CrawlJob, job_id)
            assert job is not None  # _claim 成功说明存在
            logger.info(
                "开始执行素材抓取任务 | id=%s | 平台=%s | 模式=%s",
                job.id,
                job.platform,
                job.crawler_type,
            )

            launcher = self.launcher
            if launcher is None:
                # 创建任务时校验过，但排队期间用户可能把 MC 挪走了
                self._finalize(db, job, exit_code=None, aborted="no-launcher")
                return True

            self._run_job(db, job, launcher, stop_event)
        return True

    def _run_job(self, db: Session, job: CrawlJob, launcher: List[str], stop_event) -> None:
        """跑一个任务：起 MC 子进程 → 盯 jsonl 行数 → 按产物定终态。"""
        started_monotonic = time.monotonic()
        output_dir = Path(job.output_dir)
        log_path = Path(job.log_path)

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            job.error_message = f"创建输出目录失败：{output_dir}（{exc}）"
            self._finalize(db, job, exit_code=None, aborted="failed")
            return

        argv = build_argv(
            launcher,
            platform=job.platform,
            crawler_type=job.crawler_type,
            login_type=job.login_type,
            params=dict(job.params or {}),
            output_dir=str(output_dir),
            cookies=job.login_cookies or "",
        )
        logger.info("启动 MediaCrawler | job=%s | argv=%s", job.id, argv[:4])

        process = None
        log_handle = None
        try:
            # stdio 落文件而不是 PIPE：MC 持续输出，无人读取的管道会把它憋死
            log_handle = log_path.open("wb")
            process = self.popen_factory(
                argv,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                creationflags=CHILD_CREATION_FLAGS,
                cwd=str(self.mc_root),
                env=child_env(),
            )
            self._set_child_pid(db, job.id, process.pid)

            exit_code, aborted = self._wait_for_exit(
                db, job, process, started_monotonic, stop_event
            )
        except Exception as exc:  # noqa: BLE001 - 异常也要落一个明确的终态
            logger.exception("抓取任务出现异常 | job=%s", job.id)
            if process is not None and process.poll() is None:
                terminate_process_group(process.pid, self.stop_grace_seconds)
            job.error_message = f"执行异常：{exc}"
            self._finalize(db, job, exit_code=None, aborted="failed")
            return
        finally:
            if log_handle is not None:
                try:
                    log_handle.close()
                except OSError:
                    pass

        self._finalize(db, job, exit_code=exit_code, aborted=aborted)

    def _wait_for_exit(self, db, job, process, started_monotonic, stop_event):
        """盯着子进程直到退出 / 被取消 / 被停止 / 超时。

        Returns:
            (exit_code, aborted)：aborted 为 None / "cancelled" / "stopped" / "timeout"。
        """
        output_dir = Path(job.output_dir)
        last_progress_write = 0.0

        while True:
            exit_code = process.poll()
            if exit_code is not None:
                return exit_code, None

            # 取消：整组杀掉（含 MC 拉起的 CDP Chrome），已抓到的 jsonl 保留
            if self._is_cancelled(db, job.id):
                logger.info("任务被取消，终止抓取进程 | job=%s | pid=%s", job.id, process.pid)
                terminate_process_group(process.pid, self.stop_grace_seconds)
                return process.poll(), "cancelled"

            # 服务停止：同样整组杀掉
            if stop_event is not None and stop_event.is_set():
                logger.info("服务停止，终止抓取进程 | job=%s | pid=%s", job.id, process.pid)
                terminate_process_group(process.pid, self.stop_grace_seconds)
                return process.poll(), "stopped"

            # 整体硬超时
            if time.monotonic() - started_monotonic > self.timeout_seconds:
                logger.error(
                    "抓取超时，强制终止 | job=%s | pid=%s", job.id, process.pid
                )
                terminate_process_group(process.pid, self.stop_grace_seconds)
                return process.poll(), "timeout"

            now = time.monotonic()
            if now - last_progress_write >= self.progress_seconds:
                last_progress_write = now
                job.crawled_count = crawl_results.count_notes(output_dir, job.platform)
                job.elapsed_seconds = int(round(now - started_monotonic))
                db.commit()

            time.sleep(self.tick_seconds)

    # ------------------------------------------------------------------
    # 认领与状态检查
    # ------------------------------------------------------------------

    def _claim(self, db: Session, job_id: int) -> bool:
        """条件 UPDATE 抢锁：只有 pending 才能转 running，并发下天然防重。"""
        try:
            with transaction(db):
                result = db.execute(
                    update(CrawlJob)
                    .where(
                        CrawlJob.id == job_id,
                        CrawlJob.status == CrawlJobStatus.PENDING,
                    )
                    .values(status=CrawlJobStatus.RUNNING, started_at=utcnow())
                )
                return result.rowcount == 1
        except Exception:  # noqa: BLE001 - 抢锁失败只说明这条不归我们
            logger.exception("认领抓取任务失败 | id=%s", job_id)
            return False

    def _is_cancelled(self, db: Session, job_id: int) -> bool:
        """从库里读最新状态（列查询每次都真实执行 SQL，不受会话缓存影响）。"""
        status = db.execute(
            select(CrawlJob.status).where(CrawlJob.id == job_id)
        ).scalar_one_or_none()
        return status == CrawlJobStatus.CANCELLED

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------

    def _finalize(self, db: Session, job: CrawlJob, *, exit_code, aborted) -> None:
        """定终态。判定规则（以产物为准，见模块头注释）：

        - cancelled / stopped / timeout / failed：保留对应状态，重扫 jsonl
          定 note_count（取消≠清零，抓到多少算多少）；
        - 正常退出：note_count>0 → success（退出码非 0 时补一句备注）；
          note_count=0 → failed，失败摘要从日志尾部挑。
        """
        # populate_existing：取消是在另一个会话里提交的，必须读最新状态
        fresh = db.get(CrawlJob, job.id, populate_existing=True)
        if fresh is None:
            return
        job = fresh

        note_count = crawl_results.count_notes(Path(job.output_dir), job.platform)
        job.note_count = note_count
        job.crawled_count = note_count
        job.child_pid = None
        job.finished_at = utcnow()

        if job.status == CrawlJobStatus.CANCELLED:
            db.commit()
            logger.info("抓取任务已取消 | id=%s | 已抓=%s 条", job.id, note_count)
            return

        if aborted == "no-launcher":
            job.status = CrawlJobStatus.FAILED
            if not job.error_message:
                job.error_message = "未探测到可用的 MediaCrawler，无法执行抓取"
            db.commit()
            return

        if aborted == "stopped":
            job.status = CrawlJobStatus.FAILED
            job.error_message = "服务停止，任务中断"
            db.commit()
            return

        if aborted == "timeout":
            job.status = CrawlJobStatus.FAILED
            job.error_message = (
                f"抓取超时（超过 {self.timeout_seconds} 秒），已终止；"
                f"已抓到 {note_count} 条，可在结果中查看"
                if note_count
                else f"抓取超时（超过 {self.timeout_seconds} 秒），已终止"
            )
            db.commit()
            return

        if aborted == "failed":
            job.status = CrawlJobStatus.FAILED
            db.commit()
            return

        if note_count > 0:
            job.status = CrawlJobStatus.SUCCESS
            if exit_code not in (0, None):
                job.error_message = f"MediaCrawler 以退出码 {exit_code} 结束，已抓到 {note_count} 条"
            else:
                job.error_message = ""
            db.commit()
            logger.info("抓取任务结束 | id=%s | 状态=success | 笔记=%s 条", job.id, note_count)
            return

        # 没抓到任何内容：从日志尾部挑一行能说明问题的
        tail = read_log_tail(Path(job.log_path), limit=4000)
        summary = summarize_failure(tail)
        job.status = CrawlJobStatus.FAILED
        job.error_message = (
            f"{summary}\n\n{tail}" if summary else (tail or f"退出码 {exit_code}，未抓到任何内容")
        )[:4000]
        db.commit()
        logger.info("抓取任务结束 | id=%s | 状态=failed | 笔记=0", job.id)

    def _set_child_pid(self, db: Session, job_id: int, pid: Optional[int]) -> None:
        """更新当前子进程句柄（取消、超时、孤儿回收共用的唯一依据）。"""
        db.execute(update(CrawlJob).where(CrawlJob.id == job_id).values(child_pid=pid))
        db.commit()
