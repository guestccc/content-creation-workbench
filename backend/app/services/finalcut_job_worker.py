"""一键成品任务的后台工作线程。

一个线程管两个任务域：**每轮先抢 pending 文案任务（快、秒级），再抢 pending
合成任务（慢、分钟级）**。理由与混剪的串行语义一致 —— 只有一个线程写
SQLite，回避锁竞争；代价是长合成会挡住新文案任务（进度在页面上可见，
可接受）。文案优先是为了让「生成文案」这种秒级操作不被几分钟的合成卡住。

start/stop 只由 app.main 的 lifespan 调用 —— uvicorn --reload 的父进程
会 import 模块但永不调用 lifespan，模块级起线程会起两份抢同一个 SQLite。
"""

import threading
from typing import Callable, List, Optional, Tuple

from app.core.config import settings
from app.core.logging import get_logger
from app.services.finalcut_copy_runner import FinalcutCopyRunner, claim_next_pending_id
from app.services.finalcut_render_runner import (
    FinalcutRenderRunner,
    claim_next_pending_id as claim_next_render_id,
)

logger = get_logger(__name__)


class FinalcutJobWorker:
    """单工作线程：文案任务优先，其次合成任务，都没有就可中断地等待。"""

    def __init__(
        self,
        *,
        copy_claim: Callable[[], Optional[int]] = claim_next_pending_id,
        copy_runner_factory: Callable[[], FinalcutCopyRunner] = FinalcutCopyRunner,
        render_claim: Optional[Callable[[], Optional[int]]] = None,
        render_runner_factory: Optional[Callable] = None,
        poll_seconds: Optional[float] = None,
        stop_grace_seconds: Optional[float] = None,
    ) -> None:
        self._copy_claim = copy_claim
        self._copy_runner_factory = copy_runner_factory
        # 合成任务的认领与执行由模块级单例注入（finalcut_render_runner）
        self._render_claim = render_claim
        self._render_runner_factory = render_runner_factory
        self.poll_seconds = (
            poll_seconds if poll_seconds is not None else settings.FINALCUT_WORKER_POLL_SECONDS
        )
        self.stop_grace_seconds = (
            stop_grace_seconds
            if stop_grace_seconds is not None
            else settings.FINALCUT_STOP_GRACE_SECONDS
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def stop_event(self) -> threading.Event:
        """暴露停止信号，runner 据此中断当前工作。"""
        return self._stop

    def start(self) -> None:
        """启动工作线程（幂等）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="finalcut-job-worker",
            # daemon：主进程退出时不等它；lifespan shutdown 会先 stop() 有界等待
            daemon=True,
        )
        self._thread.start()
        logger.info("一键成品工作线程已启动 | 轮询间隔=%ss", self.poll_seconds)

    def stop(self, grace: Optional[float] = None) -> None:
        """停止工作线程：置位停止信号并有界等待线程退出（理由同 mix worker）。"""
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=grace if grace is not None else self.stop_grace_seconds)
        if thread.is_alive():
            logger.warning("一键成品工作线程未在宽限期内退出，交由 daemon 兜底")
        else:
            logger.info("一键成品工作线程已停止")
        self._thread = None

    def _handlers(self) -> List[Tuple[Callable[[], Optional[int]], Callable]]:
        """(认领函数, runner) 列表，顺序即优先级：文案在前，合成在后。"""
        handlers: List[Tuple[Callable[[], Optional[int]], Callable]] = [
            (self._copy_claim, self._copy_runner_factory())
        ]
        if self._render_claim is not None and self._render_runner_factory is not None:
            handlers.append((self._render_claim, self._render_runner_factory()))
        return handlers

    def _loop(self) -> None:
        """工作循环：每轮按优先级认领一类任务执行，都没有就可中断地等待。"""
        handlers = self._handlers()
        while not self._stop.is_set():
            try:
                claimed = False
                for claim, runner in handlers:
                    job_id = claim()
                    if job_id is None:
                        continue
                    runner.run_job(job_id, stop_event=self._stop)
                    claimed = True
                    break  # 执行完一个就回到循环头：文案优先每轮都重新评估
                if not claimed:
                    self._stop.wait(self.poll_seconds)
            except Exception:  # noqa: BLE001 - 任何异常都不能让线程静默死亡
                logger.exception("一键成品工作循环出现异常，继续运行")
                self._stop.wait(self.poll_seconds)


# 模块级单例：只在 app.main 的 lifespan 里 start/stop，别处不要动它
finalcut_job_worker = FinalcutJobWorker(
    render_claim=claim_next_render_id,
    render_runner_factory=FinalcutRenderRunner,
)
