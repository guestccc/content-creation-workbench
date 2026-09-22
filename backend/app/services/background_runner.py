"""换背景任务的执行层：逐张抠图 → 合成 → 落盘。

设计要点（与 subtitle_runner.py 同构，但少了子进程那一整块）：

- 服务层管状态机与查询；本模块管「真正把一张原图变成一张换好底的 PNG」；
- 本模块不依赖 FastAPI，所有外部依赖（会话工厂、算法函数、像素上限）都可注入，
  测试传一个假算法就能在主线程同步跑完整个流程，不起线程、不读真图片。

**与 subtitle / finalcut 的结构性差别 —— 这里没有子进程**：

抠图是纯 numpy 运算，跑在 worker 线程自己的进程里。由此派生三条必须写明的语义：

1. **取消不是即时的**。没有子进程可杀，能做的只是「等这一张算完再收手」，
   所以**取消延迟的上界 = 单张图的处理时间**（大图上可能是十几秒）。
   这不是实现偷懒，是算法形态决定的；页面上要如实告知，别让用户以为点了没反应。
2. **没有 `child_pid` 列**，`recover_interrupted_jobs()` 也不需要核对进程身份 ——
   进程没了，任务就真的停了，不存在「孤儿进程还在啃 CPU」的问题。
3. **没有退出码可交叉验证**，判成功的依据只有「本张无异常 + 产物文件存在且非空」。

**单实例部署的前提**（与所有「DB 即队列」的域共享，但这里没有任何进程身份校验
兜底，所以更要说清）：`recover_interrupted_jobs()` 只看状态列，认不出「这条 running
是别的进程正在跑的」。两个后端进程共用同一个 db 文件时，后启动的那个会把前一个
正在跑的任务判成 failed。本项目是单机单实例工具（HOST 固定 127.0.0.1），不构成
实际问题；真要起了两个，先解决部署而不是改这里。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal, transaction
from app.models.background_job import (
    BackgroundJob,
    BackgroundJobItem,
    BackgroundJobItemStatus,
    BackgroundJobStatus,
)
from app.models.content import utcnow
from app.services import cutout
from app.services.cutout import CutoutError, CutoutParams

logger = get_logger(__name__)

#: 失败原因落库的最大长度。原样存整段 traceback 只会把列表接口撑大，用户也不看。
ERROR_MAX_CHARS = 500

#: 算法函数的签名：render_fn(source, page, params, *, max_pixels) -> (image, stats)
RenderFn = Callable[..., "tuple[Any, dict]"]


def _jsonable(value: Any) -> Any:
    """把诊断统计洗成能直接塞进 JSON 列的类型。

    算法的 stats 目前全是原生类型，但里面只要混进一个 numpy 标量
    （`np.float32` 不是 `float` 的子类、`np.bool_` 不是 `bool`），commit 时就会在
    `json.dumps` 上炸掉，而那时候**图其实已经算好了** —— 用户看到的是「处理失败」，
    产物却躺在磁盘上，最难查的一类假失败。所以在边界处兜一道。

    **不能让 numpy 标量直接退化成字符串**：`np.float32(0.42)` 变成 `"0.42"` 确实
    不崩，但前端拿到的是一段文本 —— 诊断数字的比较、排序、百分比换算全都会走偏，
    而且问题要到用户盯着「可见占比 0.42%」发呆时才发现。所以标量一律用 numpy
    自己的 `.item()` 拆成原生类型，数组走 `.tolist()`；真正认不出来的类型才降级
    成字符串（诊断数字而已，不值得为它把一张已经算好的图判失败）。
    """
    if isinstance(value, np.generic):        # numpy 标量：float32 / int64 / bool_ …
        return _jsonable(value.item())
    if isinstance(value, np.ndarray):
        return _jsonable(value.tolist())
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


# --------------------------------------------------------------------------
# 队列认领（DB 即队列：pending 行就是待执行任务）
# --------------------------------------------------------------------------


def claim_next_pending_id(session_factory: sessionmaker = SessionLocal) -> Optional[int]:
    """取下一个待执行任务的 ID（只读不改状态，真正抢锁在 run_job 里）。"""
    with session_factory() as db:
        row = db.execute(
            select(BackgroundJob.id)
            .where(BackgroundJob.status == BackgroundJobStatus.PENDING)
            .order_by(BackgroundJob.id.asc())
            .limit(1)
        ).first()
        return int(row[0]) if row else None


def recover_interrupted_jobs(session_factory: sessionmaker = SessionLocal) -> int:
    """启动时回收上次异常退出留下的 running 任务。

    这里没有子进程要杀（见模块 docstring），要做的只有把状态摆正：
    - 任务标记 failed；
    - running 条目标记 failed（原因写「可直接重新发起」）；
    - pending 条目标记 skipped —— **这一步不能省**，否则详情页会出现
      「任务已失败，里面却躺着一堆排队中」的自相矛盾状态；
    - pending 任务不动：DB 队列会自动接手，不需要恢复代码。

    与其余任务域一样是 best-effort：回收失败只记日志，绝不阻断服务启动。

    Returns:
        回收的任务数。
    """
    recovered = 0

    with session_factory() as db:
        try:
            with transaction(db):
                jobs = list(
                    db.execute(
                        select(BackgroundJob).where(
                            BackgroundJob.status == BackgroundJobStatus.RUNNING
                        )
                    )
                    .scalars()
                    .all()
                )
                for job in jobs:
                    logger.warning("回收中断的换背景任务 | id=%s", job.id)
                    job.status = BackgroundJobStatus.FAILED
                    job.error_message = "服务停止或重启，任务中断（可直接重新发起）"
                    job.finished_at = utcnow()
                    job.current_image = ""
                    job.current_elapsed_seconds = 0
                    for item in job.items:
                        if item.status == BackgroundJobItemStatus.RUNNING:
                            item.status = BackgroundJobItemStatus.FAILED
                            item.error_message = "服务停止或重启，任务中断（可直接重新发起）"
                            item.finished_at = utcnow()
                        elif item.status == BackgroundJobItemStatus.PENDING:
                            item.status = BackgroundJobItemStatus.SKIPPED
                            item.error_message = "服务停止或重启，未执行"
                    recovered += 1
                db.flush()
        except Exception:  # noqa: BLE001 - 启动恢复绝不能阻断服务启动
            logger.exception("回收中断任务失败")
            return 0

    if recovered:
        logger.info("已回收中断的换背景任务 | 数量=%s", recovered)
    return recovered


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------


class BackgroundRunner:
    """换背景执行器：逐张原图抠图并贴到背景上。

    所有外部依赖都可注入，测试传一个假 `render_fn` 即可在主线程同步跑完，
    不需要线程也不需要真图片。
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker = SessionLocal,
        render_fn: Optional[RenderFn] = None,
        load_page_fn: Optional[Callable[..., Any]] = None,
        max_pixels: Optional[int] = None,
    ) -> None:
        """初始化执行器。

        Args:
            session_factory: 数据库会话工厂（执行线程自己开会话，与请求线程隔离）。
            render_fn: 单张图的算法入口，默认 `cutout.render_one`；测试注入假实现。
            load_page_fn: 背景图读取入口，默认 `cutout.load_page`；测试注入假实现。
            max_pixels: 单张图的像素上限，默认取配置。
        """
        self.session_factory = session_factory
        self.render_fn: RenderFn = render_fn or cutout.render_one
        self.load_page_fn = load_page_fn or cutout.load_page
        self.max_pixels = (
            max_pixels if max_pixels is not None else settings.BACKGROUND_MAX_PIXELS
        )

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run_job(self, job_id: int, stop_event=None) -> bool:
        """认领并执行一个任务。

        Args:
            job_id: 任务 ID。
            stop_event: 工作线程的停止信号（threading.Event），置位时尽快收尾。

        Returns:
            True 表示执行了该任务；False 表示任务不存在、已被取消或已被抢占。
        """
        with self.session_factory() as db:
            if not self._claim(db, job_id):
                return False

        with self.session_factory() as db:
            job = db.get(BackgroundJob, job_id)
            assert job is not None  # _claim 成功说明存在
            params = CutoutParams.from_mapping(job.params)
            logger.info(
                "开始执行换背景任务 | id=%s | 图片数=%s | 背景=%s",
                job.id,
                job.total_images,
                Path(job.background_path).name,
            )

            # 背景图整条任务只读一次：每张都重读等于把同一张大图解码 N 遍，
            # 而它在这条任务的执行期间不会变（创建时已固化路径）。
            try:
                page = self.load_page_fn(
                    Path(job.background_path), max_pixels=self.max_pixels
                )
            except CutoutError as exc:
                # 创建任务时校验过存在性与后缀，但排队期间用户可能把它删了 /
                # 换成了一张损坏的文件。整条任务直接判失败，别让每张图各失败
                # 一次同样的原因。
                self._fail_all(db, job, f"背景图读不出来：{exc}")
                # _fail_all 只标了条目，任务自己的状态还要 _finalize 来落 ——
                # 否则任务会永远停在 running。
                self._finalize(db, job_id)
                return True

            for item in job.items:
                if item.status != BackgroundJobItemStatus.PENDING:
                    continue

                # 取消 / 服务停止：后续张标记 skipped，保留已产出的图。
                # 两种原因必须分开写 —— 服务停止时标成「任务已取消」，用户会以为
                # 谁点了取消，对真正的中断原因（如热重启）毫无头绪。
                # 顺序也是写死的：取消优先于服务停止，两个信号会同时置位。
                if self._is_cancelled(db, job_id):
                    self._skip_item(db, item, reason="任务已取消")
                    continue
                if stop_event is not None and stop_event.is_set():
                    self._skip_item(db, item, reason="服务停止或重启，未执行")
                    continue

                self._run_item(db, job, item, page, params)

            self._finalize(db, job_id)
        return True

    # ------------------------------------------------------------------
    # 认领与状态检查
    # ------------------------------------------------------------------

    def _claim(self, db: Session, job_id: int) -> bool:
        """条件 UPDATE 抢锁：只有 pending 才能转 running，并发下天然防重。"""
        try:
            with transaction(db):
                result = db.execute(
                    update(BackgroundJob)
                    .where(
                        BackgroundJob.id == job_id,
                        BackgroundJob.status == BackgroundJobStatus.PENDING,
                    )
                    .values(status=BackgroundJobStatus.RUNNING, started_at=utcnow())
                )
                return result.rowcount == 1
        except Exception:  # noqa: BLE001 - 抢锁失败只说明这条不归我们
            logger.exception("认领换背景任务失败 | id=%s", job_id)
            return False

    def _is_cancelled(self, db: Session, job_id: int) -> bool:
        """从库里读最新状态。

        必须走**列查询**：会话是 `expire_on_commit=False`，`db.get()` 拿到的对象
        在别的会话改了状态之后依然是旧值，用它判断等于永远看不见用户的取消。
        """
        status = db.execute(
            select(BackgroundJob.status).where(BackgroundJob.id == job_id)
        ).scalar_one_or_none()
        return status == BackgroundJobStatus.CANCELLED

    # ------------------------------------------------------------------
    # 单张原图
    # ------------------------------------------------------------------

    def _run_item(
        self,
        db: Session,
        job: BackgroundJob,
        item: BackgroundJobItem,
        page: Any,
        params: CutoutParams,
    ) -> None:
        """处理一张原图：抠图 → 合成 → 原子落盘 → 按产物定结果。"""
        started_monotonic = time.monotonic()
        output_path = Path(item.output_path)

        item.status = BackgroundJobItemStatus.RUNNING
        item.started_at = utcnow()
        job.current_index = item.index
        job.current_image = item.source_name
        # 计时从零开始：这个字段说的是「当前这张跑了多久」，换了图就得清零
        job.current_elapsed_seconds = 0
        db.commit()

        logger.info("开始换背景 | job=%s | item=%s | %s", job.id, item.index, item.source_name)

        # 每张一个独立的 try：一张失败不中断整批（一批 200 张里有一张过曝是常态）
        try:
            image, stats = self.render_fn(
                Path(item.source_path), page, params, max_pixels=self.max_pixels
            )
            size = self._write_png(image, output_path)
        except CutoutError as exc:
            # 算法自己判定的「这张做不了」，文案已经是给用户看的中文
            self._finish_item(
                db, job, item,
                status=BackgroundJobItemStatus.FAILED,
                started_monotonic=started_monotonic,
                error=str(exc),
            )
            return
        except Exception as exc:  # noqa: BLE001 - 单张失败不能中断整个批次
            logger.exception("换背景出现异常 | job=%s | item=%s", job.id, item.index)
            self._finish_item(
                db, job, item,
                status=BackgroundJobItemStatus.FAILED,
                started_monotonic=started_monotonic,
                error=f"处理异常：{exc}",
            )
            return

        self._finish_item(
            db, job, item,
            status=BackgroundJobItemStatus.SUCCESS,
            started_monotonic=started_monotonic,
            error="",
            stats=_jsonable(stats),
            width=image.width,
            height=image.height,
            size_bytes=size,
        )

    @staticmethod
    def _write_png(image: Any, output_path: Path) -> int:
        """把产物写成 PNG，返回字节数；写不出来 / 落不下盘就抛 CutoutError。

        **先写临时文件再 `replace()` 原子替换**：`stop()` 的有界等待只有几秒，
        服务被硬停时正在写的那半截 PNG 会留在输出目录里 —— 用户打开是一张残缺的
        图，比没有更糟。原子替换保证「产物路径上的文件要么是上一版完整的、要么是
        这一版完整的」，半成品只存在于 `.part` 那个隐藏名字下。

        显式传 `format="PNG"`：临时文件名不以 .png 结尾，靠扩展名猜格式会失败。
        """
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CutoutError(
                f"创建输出目录失败：{output_path.parent}（{exc}）"
            ) from exc

        tmp_path = output_path.with_name(f".{output_path.name}.part")
        try:
            image.save(tmp_path, format="PNG")
            tmp_path.replace(output_path)
        except OSError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass  # 清理失败就算了，别拿它盖住真正的错误
            raise CutoutError(f"写出产物失败：{output_path.name}（{exc}）") from exc

        try:
            size = output_path.stat().st_size
        except OSError as exc:
            raise CutoutError(f"产物没有落盘：{output_path.name}（{exc}）") from exc
        if size <= 0:
            # 没有子进程、没有退出码，产物是唯一的判据。空文件算失败，
            # 否则页面上会出现一张点不开的「成功」产物。
            raise CutoutError(f"产物是空文件：{output_path.name}")
        return size

    # ------------------------------------------------------------------
    # 落库小工具
    # ------------------------------------------------------------------

    def _fail_all(self, db: Session, job: BackgroundJob, reason: str) -> None:
        """把任务里所有未完成的张标记为失败（整条任务级的前置条件不满足）。"""
        for item in job.items:
            if item.status != BackgroundJobItemStatus.PENDING:
                continue
            item.status = BackgroundJobItemStatus.FAILED
            item.error_message = reason[:ERROR_MAX_CHARS]
            item.finished_at = utcnow()
        job.error_message = reason[:ERROR_MAX_CHARS]
        db.commit()

    def _finish_item(
        self,
        db: Session,
        job: BackgroundJob,
        item: BackgroundJobItem,
        *,
        status: str,
        started_monotonic: float,
        error: str,
        stats: Optional[dict] = None,
        width: int = 0,
        height: int = 0,
        size_bytes: int = 0,
    ) -> None:
        """收尾一张原图：写结果、累计任务级计数。"""
        item.status = status
        item.error_message = error[:ERROR_MAX_CHARS] if error else ""
        item.stats = stats or {}
        item.width = width
        item.height = height
        item.size_bytes = size_bytes
        item.elapsed_seconds = int(round(time.monotonic() - started_monotonic))
        item.finished_at = utcnow()

        job.completed_images += 1
        if status == BackgroundJobItemStatus.FAILED:
            job.failed_images += 1
        # 「当前这张」的计时同步落一次终值：页面在任务结束前就能看到这张用了多久
        job.current_elapsed_seconds = item.elapsed_seconds
        db.commit()

        logger.info(
            "换背景完成 | job=%s | item=%s | 状态=%s | %s×%s | 耗时=%ss",
            job.id, item.index, status, width, height, item.elapsed_seconds,
        )

    def _skip_item(self, db: Session, item: BackgroundJobItem, *, reason: str) -> None:
        """把一张未执行的原图标记为 skipped。"""
        item.status = BackgroundJobItemStatus.SKIPPED
        item.error_message = reason
        item.finished_at = utcnow()
        db.commit()

    def _finalize(self, db: Session, job_id: int) -> None:
        """汇总任务终态：取消保持 cancelled，否则按条目结果定 success/partial/failed。"""
        # populate_existing：取消是在另一个会话里提交的，这里必须读最新状态，
        # 否则会把 cancelled 覆盖成 failed
        job = db.get(BackgroundJob, job_id, populate_existing=True)
        if job is None:
            return

        if job.status == BackgroundJobStatus.CANCELLED:
            job.skipped_images = sum(
                1 for item in job.items if item.status == BackgroundJobItemStatus.SKIPPED
            )
            self._clear_current_progress(job)
            db.commit()
            logger.info("换背景任务已取消 | id=%s", job_id)
            return

        failed = sum(
            1 for item in job.items if item.status == BackgroundJobItemStatus.FAILED
        )
        skipped = sum(
            1 for item in job.items if item.status == BackgroundJobItemStatus.SKIPPED
        )
        succeeded = job.total_images - failed - skipped

        if failed == 0 and skipped == 0:
            job.status = BackgroundJobStatus.SUCCESS
        elif succeeded > 0:
            job.status = BackgroundJobStatus.PARTIAL
        else:
            job.status = BackgroundJobStatus.FAILED

        job.failed_images = failed
        job.skipped_images = skipped
        if failed:
            # 只取前三条：失败原因汇总得太长，列表里根本显示不下
            reasons = [
                f"{item.source_name}：{item.error_message.splitlines()[0] if item.error_message else '失败'}"
                for item in job.items
                if item.status == BackgroundJobItemStatus.FAILED
            ][:3]
            job.error_message = "；".join(reasons)[:ERROR_MAX_CHARS]
        self._clear_current_progress(job)
        job.finished_at = utcnow()
        db.commit()

        logger.info(
            "换背景任务结束 | id=%s | 状态=%s | 成功=%s | 失败=%s | 跳过=%s",
            job.id, job.status, succeeded, failed, skipped,
        )

    @staticmethod
    def _clear_current_progress(job: BackgroundJob) -> None:
        """任务结束时抹掉「当前这张」那一组字段。

        不抹的话，页面上会留着一条永远停在「处理中 00:12」的实时进度，
        而任务其实早就结束了。前端只在 running 时渲染这组字段，但接口数据
        本身也该是自洽的：任务不在跑了，就没有「当前这张」。
        """
        job.current_image = ""
        job.current_elapsed_seconds = 0
