"""一键成品·合成任务的执行层：把勾选的文案逐条烧进视频画面。

与 MixRunner 同构（service 管状态机，本模块管「真正把成片渲出来」），
但只有单遍：每条 item 一次 ffmpeg（drawtext 烧字 + 视频重编码）。

与混剪的三点差异：
- **输入是一条视频 + N 条文案**，不是片段拼接；没有归一化 pass；
- **滤镜串里的字体/文案是 cwd 相对路径**（font.ttf / copy.txt），子进程以
  `cwd=任务临时目录` 启动 —— 彻底绕开 Windows 盘符冒号与中文路径的转义
  （思路与混剪的 write_concat_list 一脉相承）；
- **进度分母就是视频时长**：整条视频过一遍滤镜，out_time_us / 时长即进度。

子进程管理沿用四铁律（stdio 落文件、start_new_session 整组 kill、字节捕获
自解码、-version 自证），实现都在 media_tools。
"""

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger
from app.core.materials import FINALCUT, subdir
from app.db.session import SessionLocal, transaction
from app.models.content import utcnow
from app.models.finalcut_job import (
    FinalcutItemStatus,
    FinalcutRenderItem,
    FinalcutRenderJob,
    FinalcutRenderJobStatus,
)
from app.services.finalcut_env import probe_drawtext
from app.services.finalcut_render import (
    box_to_pixels,
    build_drawtext_filter,
    build_render_argv,
    fit_font_size,
    get_style,
    wrap_text,
    write_drawtext_files,
)
from app.services.media_tools import (
    CHILD_CREATION_FLAGS,
    child_env,
    find_tool,
    read_log_tail,
    terminate_process_group,
    verify_tool,
)
from app.services.mix_runner import is_our_child, read_ffmpeg_progress_us

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# 队列认领与孤儿回收（DB 即队列）
# --------------------------------------------------------------------------


def claim_next_pending_id(session_factory: sessionmaker = SessionLocal) -> Optional[int]:
    """取下一个待执行合成任务的 ID（只读不改状态，真正抢锁在 run_job 里）。"""
    with session_factory() as db:
        row = db.execute(
            select(FinalcutRenderJob.id)
            .where(FinalcutRenderJob.status == FinalcutRenderJobStatus.PENDING)
            .order_by(FinalcutRenderJob.id.asc())
            .limit(1)
        ).first()
        return int(row[0]) if row else None


def recover_interrupted_render_jobs(
    session_factory: sessionmaker = SessionLocal,
    *,
    stop_grace_seconds: Optional[float] = None,
) -> int:
    """启动时回收上次异常退出留下的 running 合成任务（与混剪同一套做法）。

    child_pid 先过 is_our_child 核对（防 PID 复用误杀），确认是 ffmpeg 才整组杀。
    """
    grace = (
        stop_grace_seconds
        if stop_grace_seconds is not None
        else settings.FINALCUT_STOP_GRACE_SECONDS
    )
    recovered = 0

    with session_factory() as db:
        try:
            with transaction(db):
                jobs = list(
                    db.execute(
                        select(FinalcutRenderJob).where(
                            FinalcutRenderJob.status == FinalcutRenderJobStatus.RUNNING
                        )
                    )
                    .scalars()
                    .all()
                )
                for job in jobs:
                    if job.child_pid and is_our_child(job.child_pid):
                        logger.warning(
                            "回收残留的 ffmpeg 进程组 | job=%s | pid=%s",
                            job.id, job.child_pid,
                        )
                        terminate_process_group(job.child_pid, grace)
                    job.status = FinalcutRenderJobStatus.FAILED
                    job.error_message = "服务重启，任务中断"
                    job.finished_at = utcnow()
                    job.child_pid = None
                    for item in job.items:
                        if item.status == FinalcutItemStatus.RUNNING:
                            item.status = FinalcutItemStatus.FAILED
                            item.error_message = "服务重启，任务中断"
                            item.finished_at = utcnow()
                        elif item.status == FinalcutItemStatus.PENDING:
                            item.status = FinalcutItemStatus.SKIPPED
                            item.error_message = "服务重启，任务中断"
                            item.finished_at = utcnow()
                    recovered += 1
                db.flush()
        except Exception:  # noqa: BLE001 - 启动恢复绝不能阻断服务启动
            logger.exception("回收中断的合成任务失败")
            return 0

    if recovered:
        logger.info("已回收中断的合成任务 | 数量=%s", recovered)
    return recovered


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------


class FinalcutRenderRunner:
    """合成执行器：逐条把文案烧进画面，进度与结果落库。

    所有外部依赖都可注入，测试传 FakeFfmpeg + tick_seconds=0 即可在主线程
    同步跑完，不需要线程也不需要真视频。
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker = SessionLocal,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        tick_seconds: Optional[float] = None,
        progress_seconds: Optional[float] = None,
        item_timeout_seconds: Optional[int] = None,
        stop_grace_seconds: Optional[float] = None,
    ) -> None:
        self.session_factory = session_factory
        self.popen_factory = popen_factory
        self.tick_seconds = (
            tick_seconds if tick_seconds is not None else settings.FINALCUT_TICK_SECONDS
        )
        self.progress_seconds = (
            progress_seconds
            if progress_seconds is not None
            else settings.FINALCUT_PROGRESS_SECONDS
        )
        self.item_timeout_seconds = (
            item_timeout_seconds
            if item_timeout_seconds is not None
            else settings.FINALCUT_ITEM_TIMEOUT_SECONDS
        )
        self.stop_grace_seconds = (
            stop_grace_seconds
            if stop_grace_seconds is not None
            else settings.FINALCUT_STOP_GRACE_SECONDS
        )

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run_job(self, job_id: int, stop_event=None) -> bool:
        """认领并执行一个合成任务。Returns: True 表示执行了该任务。"""
        with self.session_factory() as db:
            if not self._claim(db, job_id):
                return False

        with self.session_factory() as db:
            job = db.get(FinalcutRenderJob, job_id)
            assert job is not None  # _claim 成功说明存在
            logger.info(
                "开始执行合成任务 | id=%s | 成片数=%s | 视频=%s",
                job.id, job.total_items, Path(job.video_path).name,
            )
            tmp_dir = self._tmp_dir(job.id)
            try:
                self._run(db, job, tmp_dir, stop_event)
            finally:
                self._cleanup_tmp(db, job, tmp_dir)
            self._finalize(db, job_id)
        return True

    def _run(self, db: Session, job: FinalcutRenderJob, tmp_dir: Path, stop_event) -> None:
        """前置环境检查 → 逐条烧字。环境/素材问题整任务失败，不报成素材的锅。"""
        # ---- 前置：ffmpeg 自证 + drawtext 实测 ----
        # 没有这一关，「名字叫 ffmpeg 的别的东西」会让每条都失败，报出来的却是
        # 文案的问题（混剪在 ffprobe 上踩过同样的坑）。
        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg or not verify_tool("ffmpeg", ffmpeg):
            self._fail_whole_job(
                db, job,
                "ffmpeg 不可用（未安装，或同名的文件并不是真正的 ffmpeg）；"
                "请到页面上方的环境自检查看修复建议",
            )
            return
        caps = probe_drawtext(ffmpeg)
        if not caps["ok"]:
            reason = caps["detail"] or "这个 ffmpeg 构建里没有 drawtext 滤镜"
            if caps["fix_hint"]:
                reason = f"{reason}；{caps['fix_hint']}"
            self._fail_whole_job(db, job, reason)
            return

        # ---- 前置：字体与素材（创建时探测/校验过，这里复核排队期间的变动）----
        font = Path(job.font_file) if job.font_file else None
        if font is None or not font.is_file():
            self._fail_whole_job(
                db, job,
                f"烧字用的中文字体不存在：{job.font_file or '（创建时未探测到）'}。"
                "请安装中文字体，或在 backend/.env 里用 FINALCUT_FONT_FILE 指定",
            )
            return
        src = Path(job.video_path)
        if not src.is_file():
            self._fail_whole_job(
                db, job, f"成片视频不存在（可能已被移动或删除）：{job.video_path}"
            )
            return
        spec = dict(job.video_spec or {})
        if not spec.get("width") or not spec.get("height"):
            self._fail_whole_job(db, job, "任务缺少视频规格快照，无法换算框选坐标")
            return

        tmp_dir.mkdir(parents=True, exist_ok=True)

        # ---- 逐条烧字 ----
        for item in job.items:
            if item.status != FinalcutItemStatus.PENDING:
                continue
            if self._is_cancelled(db, job.id):
                self._skip_pending_items(db, job, reason="任务已取消")
                return
            if stop_event is not None and stop_event.is_set():
                self._skip_pending_items(db, job, reason="服务停止，任务中断")
                return
            self._render_item(db, job, item, ffmpeg, caps, font, src, tmp_dir, stop_event)

    # ------------------------------------------------------------------
    # 单条成片
    # ------------------------------------------------------------------

    def _render_item(
        self, db: Session, job: FinalcutRenderJob, item: FinalcutRenderItem,
        ffmpeg: str, caps: dict, font: Path, src: Path, tmp_dir: Path, stop_event,
    ) -> None:
        """烧一条文案：排版 → 写 drawtext 文件 → ffmpeg → 原子改名就位。"""
        started = time.monotonic()
        item.status = FinalcutItemStatus.RUNNING
        item.started_at = utcnow()
        job.current_index = item.index
        job.progress_percent = 0.0
        db.commit()

        out_dir = Path(job.output_dir)
        final_path = out_dir / f"{item.index:02d}.mp4"
        # 半成品与成片同目录（os.replace 不跨卷）；它永远不会以成片名出现
        partial_path = out_dir / f"{item.index:02d}.partial.mp4"
        progress_path = tmp_dir / "progress.txt"
        log_path = tmp_dir / f"ffmpeg_item_{item.index:02d}.log"

        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            progress_path.unlink(missing_ok=True)  # 清掉上一条的进度，别读到旧值

            # 排版：手动字号尊重用户值（仍按框宽折行），0 = 自动适配
            spec = dict(job.video_spec or {})
            px_box = box_to_pixels(dict(item.box or {}), spec)
            style = get_style(item.style)
            truncated = False
            if item.font_size > 0:
                font_size = item.font_size
                lines = wrap_text(item.copy_text, px_box[2], font_size)
            else:
                font_size, lines, truncated = fit_font_size(
                    item.copy_text, px_box[2], px_box[3],
                    line_spacing=style.line_spacing,
                )
            item.resolved_font_size = font_size
            write_drawtext_files(tmp_dir, font, "\n".join(lines))
            filter_str = build_drawtext_filter(
                px_box, font_size, style,
                supports_boxborderw=bool(caps.get("supports_boxborderw")),
            )

            argv = build_render_argv(
                ffmpeg, src, partial_path, filter_str, progress_path,
                audio_codec=str(spec.get("audio_codec") or ""),
            )
            exit_code, aborted = self._run_ffmpeg(
                db, job, argv, log_path, tmp_dir, started,
                timeout_seconds=self.item_timeout_seconds,
                stop_event=stop_event,
                progress_hint=job.video_duration,
            )

            if aborted == "cancelled":
                partial_path.unlink(missing_ok=True)
                self._finish_item(
                    db, job, item, status=FinalcutItemStatus.SKIPPED,
                    started_monotonic=started, exit_code=exit_code, error="任务已取消",
                )
                return
            if aborted == "stopped":
                partial_path.unlink(missing_ok=True)
                self._finish_item(
                    db, job, item, status=FinalcutItemStatus.FAILED,
                    started_monotonic=started, exit_code=exit_code,
                    error="服务停止，任务中断",
                )
                return
            if aborted == "timeout":
                partial_path.unlink(missing_ok=True)
                self._finish_item(
                    db, job, item, status=FinalcutItemStatus.FAILED,
                    started_monotonic=started, exit_code=exit_code,
                    error=f"合成超时（超过 {self.item_timeout_seconds} 秒），已终止",
                )
                return

            if exit_code != 0 or not partial_path.is_file():
                tail = read_log_tail(log_path)
                partial_path.unlink(missing_ok=True)
                self._finish_item(
                    db, job, item, status=FinalcutItemStatus.FAILED,
                    started_monotonic=started, exit_code=exit_code,
                    error=tail[-300:] if tail else f"ffmpeg 退出码 {exit_code}，未产出成片",
                )
                return

            # 半成品永远不会以成片的文件名出现：先 .partial 再原子改名
            os.replace(partial_path, final_path)
            item.output_path = str(final_path)
            item.output_name = final_path.name
            item.duration_seconds = job.video_duration
            item.size_bytes = final_path.stat().st_size
            # 文案过长被截断：任务仍算成功，但原因写进该条，用户能看到
            note = "文案超出框选区域，已按最小字号截断（可放大框或缩短文案后重试）" if truncated else ""
            self._finish_item(
                db, job, item, status=FinalcutItemStatus.SUCCESS,
                started_monotonic=started, exit_code=exit_code, error=note,
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不能中断整个批次
            logger.exception("成片合成出现异常 | job=%s | item=%s", job.id, item.index)
            partial_path.unlink(missing_ok=True)
            self._finish_item(
                db, job, item, status=FinalcutItemStatus.FAILED,
                started_monotonic=started, exit_code=None, error=f"合成异常：{exc}",
            )

    # ------------------------------------------------------------------
    # 子进程执行与等待
    # ------------------------------------------------------------------

    def _run_ffmpeg(
        self, db: Session, job: FinalcutRenderJob, argv, log_path: Path,
        tmp_dir: Path, started: float,
        *, timeout_seconds: int, stop_event, progress_hint: float = 0.0,
    ) -> tuple:
        """起一个 ffmpeg 子进程并盯到退出/取消/停止/超时。

        Returns:
            (exit_code, aborted)：aborted ∈ None/"cancelled"/"stopped"/"timeout"。
        """
        log_handle = None
        process = None
        try:
            # stdio 落文件而不是 PIPE：无人读取的管道会把持续输出的子进程憋死
            log_handle = log_path.open("wb")
            process = self.popen_factory(
                argv,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                creationflags=CHILD_CREATION_FLAGS,
                env=child_env(),
                # 滤镜串里的 font.ttf / copy.txt 是相对名，靠 cwd 解析
                cwd=str(tmp_dir),
            )
            self._set_child_pid(db, job.id, process.pid)

            last_write = 0.0
            while True:
                exit_code = process.poll()
                if exit_code is not None:
                    return exit_code, None

                if self._is_cancelled(db, job.id):
                    logger.info(
                        "任务被取消，终止 ffmpeg | job=%s | pid=%s", job.id, process.pid
                    )
                    terminate_process_group(process.pid, self.stop_grace_seconds)
                    return process.poll(), "cancelled"
                if stop_event is not None and stop_event.is_set():
                    terminate_process_group(process.pid, self.stop_grace_seconds)
                    return process.poll(), "stopped"
                if time.monotonic() - started > timeout_seconds:
                    terminate_process_group(process.pid, self.stop_grace_seconds)
                    return process.poll(), "timeout"

                now = time.monotonic()
                if now - last_write >= self.progress_seconds and progress_hint > 0:
                    last_write = now
                    self._refresh_progress(db, job, tmp_dir, progress_hint)

                time.sleep(self.tick_seconds)
        finally:
            self._set_child_pid(db, job.id, None)
            if log_handle is not None:
                try:
                    log_handle.close()
                except OSError:
                    pass

    def _refresh_progress(
        self, db: Session, job: FinalcutRenderJob, tmp_dir: Path, progress_hint: float
    ) -> None:
        """把 ffmpeg 的 out_time_us 换算成当前这条成片的百分比落库。

        分母是视频时长（创建任务时已 ffprobe 过），不编造权重。
        读不到进度文件就保持上一次的值，别让数字跳。
        """
        out_us = read_ffmpeg_progress_us(tmp_dir / "progress.txt")
        if out_us is not None and progress_hint > 0:
            job.progress_percent = min(round(out_us / 1_000_000 / progress_hint * 100, 1), 100.0)
        db.commit()

    # ------------------------------------------------------------------
    # 落库小工具与状态机
    # ------------------------------------------------------------------

    def _claim(self, db: Session, job_id: int) -> bool:
        """条件 UPDATE 抢锁：只有 pending 才能转 running，并发下天然防重。"""
        try:
            with transaction(db):
                result = db.execute(
                    update(FinalcutRenderJob)
                    .where(
                        FinalcutRenderJob.id == job_id,
                        FinalcutRenderJob.status == FinalcutRenderJobStatus.PENDING,
                    )
                    .values(status=FinalcutRenderJobStatus.RUNNING, started_at=utcnow())
                )
                return result.rowcount == 1
        except Exception:  # noqa: BLE001 - 抢锁失败只说明这条不归我们
            logger.exception("认领合成任务失败 | id=%s", job_id)
            return False

    def _is_cancelled(self, db: Session, job_id: int) -> bool:
        """从库里读最新状态（列查询每次都真实执行 SQL，不受会话缓存影响）。"""
        status = db.execute(
            select(FinalcutRenderJob.status).where(FinalcutRenderJob.id == job_id)
        ).scalar_one_or_none()
        return status == FinalcutRenderJobStatus.CANCELLED

    def _set_child_pid(self, db: Session, job_id: int, pid: Optional[int]) -> None:
        """更新当前子进程句柄（取消、超时、孤儿回收共用的唯一依据）。"""
        db.execute(
            update(FinalcutRenderJob)
            .where(FinalcutRenderJob.id == job_id)
            .values(child_pid=pid)
        )
        db.commit()

    def _finish_item(
        self, db, job, item, *, status, started_monotonic, exit_code, error
    ) -> None:
        """收尾一条成片：写结果、累计任务级计数。"""
        item.status = status
        item.exit_code = exit_code
        item.error_message = error[:4000] if error else ""
        item.elapsed_seconds = int(round(time.monotonic() - started_monotonic))
        item.finished_at = utcnow()

        job.completed_items += 1
        if status == FinalcutItemStatus.FAILED:
            job.failed_items += 1
        elif status == FinalcutItemStatus.SKIPPED:
            job.skipped_items += 1
        job.child_pid = None
        db.commit()

        logger.info(
            "成片合成完成 | job=%s | item=%s | 状态=%s | 耗时=%ss",
            job.id, item.index, status, item.elapsed_seconds,
        )

    def _skip_pending_items(self, db, job, *, reason: str) -> None:
        """把剩余未执行的成片全部标记 skipped（取消/停止时的批量收尾）。"""
        for item in job.items:
            if item.status == FinalcutItemStatus.PENDING:
                item.status = FinalcutItemStatus.SKIPPED
                item.error_message = reason
                item.finished_at = utcnow()
                job.completed_items += 1
                job.skipped_items += 1
        db.commit()

    def _fail_whole_job(self, db, job, reason: str) -> None:
        """准备阶段就失败（环境/字体/素材问题）：所有成片标失败，_finalize 汇总。"""
        for item in job.items:
            if item.status == FinalcutItemStatus.PENDING:
                self._finish_item(
                    db, job, item,
                    status=FinalcutItemStatus.FAILED,
                    started_monotonic=time.monotonic(),
                    exit_code=None, error=reason,
                )

    def _tmp_dir(self, job_id: int) -> Path:
        """任务临时目录：materials/finalcut/.tmp/finalcut_<job_id>/（字体/文案/日志）。"""
        return subdir(FINALCUT) / ".tmp" / f"finalcut_{job_id}"

    def _cleanup_tmp(self, db, job, tmp_dir: Path) -> None:
        """清理临时目录。

        成功/取消 → 全删；有失败 → 整个保留（里面有 ffmpeg_*.log，排查全靠它，
        字体与 copy.txt 体积小，不值得为省这点磁盘把日志一起删了）。
        """
        # populate_existing：取消是在另一个会话里提交的，必须读最新状态
        fresh = db.get(FinalcutRenderJob, job.id, populate_existing=True)
        status = fresh.status if fresh else job.status
        try:
            has_failure = any(
                item.status == FinalcutItemStatus.FAILED for item in job.items
            )
            if status == FinalcutRenderJobStatus.CANCELLED or not has_failure:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError as exc:
            # 临时文件清不掉只是占磁盘，不该把已经跑完的任务标失败
            logger.warning("清理合成临时目录失败 | job=%s | %s", job.id, exc)

    @staticmethod
    def _clear_current_progress(job: FinalcutRenderJob) -> None:
        """任务结束时抹掉「当前正在做什么」那一组字段，保持接口数据自洽。"""
        job.current_index = 0
        job.progress_percent = 0.0

    def _finalize(self, db: Session, job_id: int) -> None:
        """汇总任务终态：取消保持 cancelled，否则按成片结果定 success/partial/failed。"""
        job = db.get(FinalcutRenderJob, job_id, populate_existing=True)
        if job is None:
            return

        if job.status == FinalcutRenderJobStatus.CANCELLED:
            job.skipped_items = sum(
                1 for item in job.items if item.status == FinalcutItemStatus.SKIPPED
            )
            job.child_pid = None
            self._clear_current_progress(job)
            db.commit()
            logger.info("合成任务已取消 | id=%s", job_id)
            return

        failed = sum(1 for item in job.items if item.status == FinalcutItemStatus.FAILED)
        skipped = sum(1 for item in job.items if item.status == FinalcutItemStatus.SKIPPED)
        succeeded = job.total_items - failed - skipped

        if failed == 0 and skipped == 0:
            job.status = FinalcutRenderJobStatus.SUCCESS
        elif succeeded > 0:
            job.status = FinalcutRenderJobStatus.PARTIAL
        else:
            job.status = FinalcutRenderJobStatus.FAILED

        job.failed_items = failed
        job.skipped_items = skipped
        if failed:
            reasons = [
                f"第{item.index}条：{item.error_message.splitlines()[0] if item.error_message else '失败'}"
                for item in job.items
                if item.status == FinalcutItemStatus.FAILED
            ][:3]
            job.error_message = "；".join(reasons)
        job.child_pid = None
        self._clear_current_progress(job)
        job.finished_at = utcnow()
        db.commit()

        logger.info(
            "合成任务结束 | id=%s | 状态=%s | 成功=%s | 失败=%s",
            job.id, job.status, succeeded, failed,
        )
