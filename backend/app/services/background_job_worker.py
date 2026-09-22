"""换背景任务的后台工作线程。

与 subtitle_job_worker.py 完全同构，刻意保持薄：
- 队列就是 background_jobs 表里 status='pending' 的行，这里没有第二份队列状态；
- 执行逻辑全在 BackgroundRunner 里，本模块只是一个循环外壳；
- start/stop 只由 lifespan 调用 —— uvicorn --reload 的父进程会 import 模块
  但永不调用 lifespan，所以 worker 放模块级会起两份、抢同一个 SQLite。

测试说明：claim_next / runner_factory 都可注入，线程用例完全不碰 DB。

**「有界等待」在这里的含义与字幕提取不同**：那边 stop() 能杀掉子进程，几秒内
一定退得出来；这边没有子进程可杀，runner 只能在当前这张图算完之后才看停止信号。
所以 `stop_grace_seconds` 小的结果是「线程没在宽限期内退出」，而不是「任务被中断」——
超时后线程仍是 daemon，随主进程退出被强杀，残留的 running 任务由下次启动的
recover_interrupted_jobs() 收成 failed。
"""

import threading
from typing import Callable, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.services.background_runner import BackgroundRunner, claim_next_pending_id

logger = get_logger(__name__)


class BackgroundJobWorker:
    """单工作线程：循环认领 pending 任务并同步执行。"""

    def __init__(
        self,
        *,
        claim_next: Callable[[], Optional[int]] = claim_next_pending_id,
        runner_factory: Callable[[], BackgroundRunner] = BackgroundRunner,
        poll_seconds: Optional[float] = None,
        stop_grace_seconds: Optional[float] = None,
    ) -> None:
        """初始化工作线程。

        Args:
            claim_next: 取下一个待执行任务 ID 的函数（可注入，测试不碰 DB）。
            runner_factory: 构造执行器的工厂（可注入）。
            poll_seconds: 空闲时的轮询间隔。
            stop_grace_seconds: stop() 等待线程退出的有界时间。
        """
        self._claim_next = claim_next
        self._runner_factory = runner_factory
        self.poll_seconds = (
            poll_seconds if poll_seconds is not None else settings.BACKGROUND_WORKER_POLL_SECONDS
        )
        self.stop_grace_seconds = (
            stop_grace_seconds
            if stop_grace_seconds is not None
            else settings.BACKGROUND_STOP_GRACE_SECONDS
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def stop_event(self) -> threading.Event:
        """暴露停止信号，runner 据此在两张图之间收手。"""
        return self._stop

    def start(self) -> None:
        """启动工作线程（幂等：lifespan 可能被调多次）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="background-job-worker",
            # daemon：主进程退出时不等它 —— 但 lifespan shutdown 会先 stop() 有界等待，
            # daemon 只是双 Ctrl-C 这类硬退出下的兜底
            daemon=True,
        )
        self._thread.start()
        logger.info("换背景工作线程已启动 | 轮询间隔=%ss", self.poll_seconds)

    def stop(self, grace: Optional[float] = None) -> None:
        """停止工作线程：置位停止信号并有界等待线程退出。

        等待必须有界 —— uvicorn reloader 的 join() 没有超时，这里若无界，改一次
        代码热重载就要卡到当前那张图算完。⚠️ 抠图没有子进程可杀，碰到一张大图时
        线程真的会超出宽限期还没退出：那是预期行为（daemon 兜底 + 下次启动回收），
        不是故障。
        """
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=grace if grace is not None else self.stop_grace_seconds)
        if thread.is_alive():
            logger.warning(
                "换背景工作线程未在宽限期内退出（当前这张图还没算完），交由 daemon 兜底"
            )
        else:
            logger.info("换背景工作线程已停止")
        self._thread = None

    def _loop(self) -> None:
        """工作循环：有任务就执行，没有就可中断地等待下一轮。"""
        runner = self._runner_factory()
        while not self._stop.is_set():
            try:
                job_id = self._claim_next()
                if job_id is None:
                    # wait 而非 sleep：stop() 能立即唤醒，不用干等一个轮询周期
                    self._stop.wait(self.poll_seconds)
                    continue
                runner.run_job(job_id, stop_event=self._stop)
            except Exception:  # noqa: BLE001 - 兜底：任何异常都不能让线程静默死亡
                # 否则之后所有任务永远 pending，而健康检查还显示正常
                logger.exception("换背景工作循环出现异常，继续运行")
                self._stop.wait(self.poll_seconds)


# 模块级单例：只在 app.main 的 lifespan 里 start/stop，别处不要动它
background_job_worker = BackgroundJobWorker()
