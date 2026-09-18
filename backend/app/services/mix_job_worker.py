"""智能混剪任务的后台工作线程。

与 scene_job_worker.py 同构：单线程循环认领 mix_jobs 表里 status='pending'
的行并同步执行。队列就是表本身，这里没有第二份队列状态。

start/stop 只由 app.main 的 lifespan 调用 —— uvicorn --reload 的父进程
会 import 模块但永不调用 lifespan，模块级起线程会起两份抢同一个 SQLite。
"""

import threading
from typing import Callable, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.services.mix_runner import MixRunner, claim_next_pending_id

logger = get_logger(__name__)


class MixJobWorker:
    """单工作线程：循环认领 pending 混剪任务并同步执行。"""

    def __init__(
        self,
        *,
        claim_next: Callable[[], Optional[int]] = claim_next_pending_id,
        runner_factory: Callable[[], MixRunner] = MixRunner,
        poll_seconds: Optional[float] = None,
        stop_grace_seconds: Optional[float] = None,
    ) -> None:
        self._claim_next = claim_next
        self._runner_factory = runner_factory
        self.poll_seconds = (
            poll_seconds if poll_seconds is not None else settings.MIX_JOB_POLL_SECONDS
        )
        self.stop_grace_seconds = (
            stop_grace_seconds
            if stop_grace_seconds is not None
            else settings.MIX_JOB_STOP_GRACE_SECONDS
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def stop_event(self) -> threading.Event:
        """暴露停止信号，runner 据此中断当前子进程。"""
        return self._stop

    def start(self) -> None:
        """启动工作线程（幂等）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="mix-job-worker",
            # daemon：主进程退出时不等它；lifespan shutdown 会先 stop() 有界等待
            daemon=True,
        )
        self._thread.start()
        logger.info("混剪工作线程已启动 | 轮询间隔=%ss", self.poll_seconds)

    def stop(self, grace: Optional[float] = None) -> None:
        """停止工作线程：置位停止信号并有界等待线程退出（理由同 scene worker）。"""
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=grace if grace is not None else self.stop_grace_seconds)
        if thread.is_alive():
            logger.warning("混剪工作线程未在宽限期内退出，交由 daemon 兜底")
        else:
            logger.info("混剪工作线程已停止")
        self._thread = None

    def _loop(self) -> None:
        """工作循环：有任务就执行，没有就可中断地等待下一轮。"""
        runner = self._runner_factory()
        while not self._stop.is_set():
            try:
                job_id = self._claim_next()
                if job_id is None:
                    self._stop.wait(self.poll_seconds)
                    continue
                runner.run_job(job_id, stop_event=self._stop)
            except Exception:  # noqa: BLE001 - 任何异常都不能让线程静默死亡
                logger.exception("混剪工作循环出现异常，继续运行")
                self._stop.wait(self.poll_seconds)


# 模块级单例：只在 app.main 的 lifespan 里 start/stop，别处不要动它
mix_job_worker = MixJobWorker()
