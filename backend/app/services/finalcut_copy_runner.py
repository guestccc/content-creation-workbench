"""一键成品·文案生成任务的执行层。

与 MixRunner 的职责划分一致：service 管状态机与查询，本模块管「真正把文案
调出来」。不依赖 FastAPI，会话工厂与 chat 函数都可注入，测试在主线程同步
跑完整个流程。

与 MixRunner 的三点不同（都源自「文案任务没有子进程」）：

1. **取消不是即时的**：没有子进程可杀，取消 = service 置状态列，本模块只在
   **相位边界**（读完字幕 / HTTP 返回 / 解析完）重读状态列并丢弃结果。
   在途 HTTP 无法中断，取消延迟上界 = AI_TIMEOUT_SECONDS（见设计文档）；
2. **没有 child_pid 与孤儿回收**：启动恢复只做一件事 —— 把 running 的
   文案任务标 failed（recover_interrupted_copy_jobs）；
3. **进度是相位跳变**：read → analyze → parse，没有可读的连续进度，
   百分比按相位如实跳变，不编造权重。

失败原因的写法：AiError 已经按 kind 翻译成人话（config/auth/rate_limit/...），
error_message 直接用它，页面看到的是「去改 key」而不是堆栈。
"""

import threading
import time
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal, transaction
from app.models.content import utcnow
from app.models.finalcut_job import FinalcutCopyJob, FinalcutCopyJobStatus, FinalcutCopyPhase
from app.services.ai_client import AiError, chat_json
from app.services.finalcut_copy import (
    build_copy_messages,
    char_budget,
    parse_copy_payload,
)
from app.services.finalcut_copy_service import read_subtitle_material

logger = get_logger(__name__)

#: error_message 列的截断长度（与其它 runner 的 _finish 一致，防超长日志塞爆）。
_ERROR_MAX_CHARS = 500

#: 相位进度：百分比按相位如实跳变（没有连续进度可编）。
_PHASE_PROGRESS = {
    FinalcutCopyPhase.READ: 10.0,
    FinalcutCopyPhase.ANALYZE: 40.0,
    FinalcutCopyPhase.PARSE: 80.0,
}


def claim_next_pending_id(session_factory: sessionmaker = SessionLocal) -> Optional[int]:
    """取下一个待执行文案任务的 ID（只读不改状态，真正抢锁在 run_job 里）。"""
    with session_factory() as db:
        row = db.execute(
            select(FinalcutCopyJob.id)
            .where(FinalcutCopyJob.status == FinalcutCopyJobStatus.PENDING)
            .order_by(FinalcutCopyJob.id.asc())
            .limit(1)
        ).first()
        return int(row[0]) if row else None


def recover_interrupted_copy_jobs(session_factory: sessionmaker = SessionLocal) -> int:
    """启动时把上次异常退出留下的 running 文案任务标 failed。

    文案任务没有子进程，不存在要杀的孤儿 ——  recovery 只是把状态说圆：
    「服务重启，任务中断」，用户重新点一次即可。
    """
    recovered = 0
    with session_factory() as db:
        try:
            with transaction(db):
                jobs = list(
                    db.execute(
                        select(FinalcutCopyJob).where(
                            FinalcutCopyJob.status == FinalcutCopyJobStatus.RUNNING
                        )
                    )
                    .scalars()
                    .all()
                )
                for job in jobs:
                    job.status = FinalcutCopyJobStatus.FAILED
                    job.error_message = "服务重启，任务中断"
                    job.finished_at = utcnow()
                    recovered += 1
        except Exception:  # noqa: BLE001 - 恢复失败不该让启动失败
            logger.exception("恢复文案任务中断状态失败")
    if recovered:
        logger.info("已恢复 %s 个中断的文案任务", recovered)
    return recovered


class FinalcutCopyRunner:
    """文案生成执行器：读字幕 → 调 AI → 解析落库。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker = SessionLocal,
        chat_fn: Callable = chat_json,
    ) -> None:
        self.session_factory = session_factory
        # AI 调用点做成可注入：测试传假函数，不必 MockTransport 绕一层
        self._chat = chat_fn

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run_job(self, job_id: int, stop_event: Optional[threading.Event] = None) -> bool:
        """认领并执行一个文案任务。Returns: True 表示执行了该任务。"""
        with self.session_factory() as db:
            if not self._claim(db, job_id):
                return False

        with self.session_factory() as db:
            job = db.get(FinalcutCopyJob, job_id)
            assert job is not None  # _claim 成功说明存在
            logger.info(
                "开始执行文案任务 | id=%s | 时长=%.1fs | 条数=%s",
                job.id, job.video_duration, job.copy_count,
            )
            started = time.monotonic()
            try:
                self._run(db, job)
            except Exception as exc:  # noqa: BLE001 - 兜底：任何意外都要落成 failed，不能挂着
                logger.exception("文案任务执行异常 | id=%s", job_id)
                self._fail(db, job, f"执行出错：{exc}")

            if job.status == FinalcutCopyJobStatus.RUNNING:
                # 既不是成功也不是失败地出来了（理论上不该发生）：落 failed 兜底
                self._fail(db, job, "执行流程异常中断")
            logger.info(
                "文案任务结束 | id=%s | 状态=%s | 耗时=%.1fs",
                job.id, job.status, time.monotonic() - started,
            )
        return True

    def _run(self, db: Session, job: FinalcutCopyJob) -> None:
        """相位推进主体。每个相位边界都有一次取消检查。"""
        # ---- read：读字幕并拆解 ----
        self._enter_phase(db, job, FinalcutCopyPhase.READ)
        try:
            material, _truncated = read_subtitle_material(Path(job.subtitle_path))
        except OSError as exc:
            self._fail(db, job, f"字幕文件读不出来：{exc}")
            return
        if not material.strip():
            self._fail(db, job, "字幕文件里没有可用文本（可能已被清空或替换）")
            return
        if self._cancelled(db, job):
            return

        # ---- analyze：调 AI ----
        self._enter_phase(db, job, FinalcutCopyPhase.ANALYZE)
        # 语速取任务上的快照（创建时落的）；0.0 = 升级前建的老任务，
        # 补列给了 0.0 而它可能还停在 pending —— recover_interrupted 只管
        # running 的，重启后照样会被认领跑起来，所以这里的回退是活路径。
        rate = job.chars_per_second or float(settings.FINALCUT_CHARS_PER_SECOND)
        budget = char_budget(job.video_duration, rate)
        # 语速与预算一起记：用户说「预算不对」时，先看这一行是语速没校准
        # 还是时长算错了，不用去猜（语速是建任务时可在页面上改的值）。
        logger.info(
            "字数预算 | id=%s | 时长=%s 秒 × 语速=%s 字/秒 → %s–%s 字",
            job.id, job.video_duration, rate, *budget,
        )
        messages = build_copy_messages(
            material, job.video_duration, job.hint, job.copy_count, budget
        )
        try:
            result = self._chat(messages)
        except AiError as exc:
            self._fail(db, job, exc.user_message)
            return
        except Exception as exc:  # noqa: BLE001 - 注入的 chat_fn 可能抛别的
            self._fail(db, job, f"AI 调用失败：{exc}")
            return
        if self._cancelled(db, job):
            return

        # ---- parse：解析与校验 ----
        self._enter_phase(db, job, FinalcutCopyPhase.PARSE)
        try:
            analysis, copies = parse_copy_payload(result.content)
        except (ValueError, AiError) as exc:
            # 原文也要留下来：解析失败的排查全靠它
            self._fail(
                db, job,
                exc.user_message if isinstance(exc, AiError) else str(exc),
                raw_response=result.content,
            )
            return
        if self._cancelled(db, job):
            return

        # ---- 落库 ----
        usage = result.usage if isinstance(result.usage, dict) else {}
        with transaction(db):
            job.status = FinalcutCopyJobStatus.SUCCESS
            job.result = {"analysis": analysis, "copies": copies}
            job.raw_response = result.content
            job.tokens_used = int(usage.get("total_tokens") or 0)
            job.progress_percent = 100.0
            job.finished_at = utcnow()
            db.add(job)
        logger.info(
            "文案任务成功 | id=%s | 有效文案=%s 条 | tokens=%s",
            job.id, len(copies), job.tokens_used,
        )

    # ------------------------------------------------------------------
    # 相位与状态
    # ------------------------------------------------------------------

    def _claim(self, db: Session, job_id: int) -> bool:
        """条件 UPDATE 抢锁：只有 pending 才能转 running，并发下天然防重。"""
        try:
            with transaction(db):
                result = db.execute(
                    update(FinalcutCopyJob)
                    .where(
                        FinalcutCopyJob.id == job_id,
                        FinalcutCopyJob.status == FinalcutCopyJobStatus.PENDING,
                    )
                    .values(status=FinalcutCopyJobStatus.RUNNING, started_at=utcnow())
                )
                return result.rowcount == 1
        except Exception:  # noqa: BLE001
            logger.exception("认领文案任务失败 | id=%s", job_id)
            return False

    def _enter_phase(self, db: Session, job: FinalcutCopyJob, phase: str) -> None:
        """推进相位并落库（轮询接口直接读这两个列）。"""
        with transaction(db):
            job.current_phase = phase
            job.progress_percent = _PHASE_PROGRESS.get(phase, 0.0)
            db.add(job)

    def _cancelled(self, db: Session, job: FinalcutCopyJob) -> bool:
        """相位边界的取消检查：状态列被置成 cancelled 就弃结果。

        取消接口已经写了 finished_at，这里什么都不用再写，只需要如实停笔。
        """
        db.refresh(job)
        if job.status != FinalcutCopyJobStatus.CANCELLED:
            return False
        logger.info("文案任务在相位边界收到取消，丢弃结果 | id=%s | phase=%s", job.id, job.current_phase)
        return True

    def _fail(
        self,
        db: Session,
        job: FinalcutCopyJob,
        message: str,
        *,
        raw_response: str = "",
    ) -> None:
        """落 failed：错误文案已按 AiError 分类翻译成人话，直接展示。"""
        with transaction(db):
            job.status = FinalcutCopyJobStatus.FAILED
            job.error_message = message[:_ERROR_MAX_CHARS]
            if raw_response:
                job.raw_response = raw_response
            job.finished_at = utcnow()
            db.add(job)
        logger.warning("文案任务失败 | id=%s | %s", job.id, job.error_message)
