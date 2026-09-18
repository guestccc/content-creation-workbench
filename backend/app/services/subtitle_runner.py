"""字幕提取任务的执行层：起 VideoCaptioner 子进程、盯进度、收尾。

设计要点（与 scene_runner.py 完全同构，只是因为被调工具不同而少了几件事）：
- service 管状态机与查询；本模块管「真正把一条视频转成字幕」；
- 本模块不依赖 FastAPI，所有外部依赖（会话工厂、Popen、时钟参数）都可注入，
  测试可以传 FakePopen 在主线程同步跑完整个流程，不起线程、不起真子进程。

子进程管理的三条硬规则（踩过坑的，别改）：
1. stdio 必须落文件，绝不 PIPE —— VideoCaptioner 转写时持续输出，无人读取的
   管道缓冲写满后子进程会永久阻塞在 write 上（任务「跑到一半不动了」）；
2. 必须 start_new_session=True 并整组 kill —— 它会拉起 ffmpeg 做音频转换，
   只杀直接子进程会留下孤儿 ffmpeg 继续啃 CPU；
3. 取消/超时/服务停止共用同一个终止函数，且等待必须有界 —— uvicorn reloader
   的 join() 没有超时，这里任何一次无界等待都会让热重载卡死。

**两条与镜头分割不同的判断**（都是读 VideoCaptioner 源码确认的）：

a. **进度只能按条算，没有百分比**。它的进度条（`output.ProgressLine`）只在
   `stderr.isatty()` 为真时才渲染，而我们把 stdio 重定向到文件 ⇒ 拿不到任何
   机器可读的进度。所以这里如实只写「第几条 + 这条跑了多久」，不编分母 ——
   与前端 SceneSplit 里「分母未知就不显示百分比」是同一个取舍。

b. **结果以产物为准，不只看退出码**。VideoCaptioner 会在收尾阶段（保存/日志）
   报错，而字幕其实已经写好了；反过来退出码 0 也可能一个文件都没有
   （比如识别出 0 条字幕）。所以判成功的依据是「目标 .srt 存在且非空」，
   退出码只用来填失败原因。
"""

import os
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
from app.models.subtitle_job import (
    SubtitleJob,
    SubtitleJobItem,
    SubtitleJobItemStatus,
    SubtitleJobStatus,
)

# 子进程工具函数与镜头分割 / 混剪共用，实现都在 media_tools
from app.services.media_tools import (
    child_env,
    command_line,
    probe_duration,
    read_log_tail,
    terminate_process_group,
)
from app.services.subtitle_env import detect

logger = get_logger(__name__)

# 子进程日志名：与字幕同目录，失败时读它的尾部给用户看真实报错
VC_LOG_NAME = "videocaptioner.log"

#: 转写参数的**白名单键**。params 来自数据库，不信任其中的额外内容：
#: 只认这三个，其余一律忽略（build_argv 里逐项取值）。
ALLOWED_PARAMS = ("asr", "language", "format")

# 失败摘要里优先挑出来的行。VideoCaptioner 的 output.error() 写的是
# `✗ Error: <msg>`，且**错误一律走 stderr** —— 我们做了 stderr→stdout 合并，
# 所以日志文件里两类输出是混在一起的，按前缀挑即可。
_ERROR_MARKERS = ("✗ Error:", "✗ ", "Error:", "error:")


def build_argv(launcher: List[str], video: Path, out_dir: Path, params: dict) -> List[str]:
    """把白名单参数映射成 VideoCaptioner transcribe 的 argv。

    几个必须照做的细节（读它的 CLI 源码确认）：
    - `-o` 必须是**已存在的目录**（调用方保证）并且带结尾斜杠：不带扩展名又
      不是目录时，它会把这个值当成**文件名**，字幕就落到一个没有扩展名的
      文件里去了；
    - `--quiet` 让它只打印结果路径，人读的输出整段省掉 —— 日志小得多；
    - `--language` 只在显式给了语言时才传，留空即「自动检测」（那是它的默认值）。

    Args:
        launcher: 调用前缀，来自 subtitle_env.detect()，形如
            `[解释器, "-m", "videocaptioner"]` 或 `["/path/videocaptioner"]`。
        video: 输入视频（绝对路径）。
        out_dir: 输出目录（已存在）。
        params: 任务参数（json 列里的字典）。
    """
    argv = list(launcher) + ["transcribe", str(video)]

    asr = params.get("asr")
    if asr:
        argv += ["--asr", str(asr)]

    language = params.get("language")
    if language:
        argv += ["--language", str(language)]

    output_format = params.get("format") or "srt"
    argv += ["--format", str(output_format)]

    # 目录必须以分隔符结尾 —— 见上面 `-o` 的说明
    argv += ["-o", str(out_dir) + os.sep, "--quiet"]
    return argv


def is_our_child(pid: int) -> bool:
    """校验一个 PID 现在确实还是我们起的 VideoCaptioner / ffmpeg 进程。

    机器重启后 PID 会被复用，孤儿回收时盲杀可能干掉无辜进程（比如用户刚
    打开的编辑器），所以回收前必须用命令行核对身份。
    """
    if pid <= 0:
        return False
    command = command_line(pid)
    if not command:
        return False
    return (
        "videocaptioner" in command
        or "-m videocaptioner" in command
        or command.lstrip().startswith("ffmpeg")
    )


def count_segments(srt_path: Path) -> int:
    """数一份 .srt 里有多少条字幕。

    srt 的块之间用空行分隔、每块第一行是序号，所以「全是数字的行」的个数
    就是字幕条数。解析不了（文件被截断、编码异常）时返回 0 —— 这是个展示用
    的统计值，不值得为它让整条任务失败。
    """
    try:
        text = srt_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return 0

    # 只统计「块首序号」：紧跟在空行之后（或文件开头）的纯数字行
    count = 0
    previous_blank = True
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            previous_blank = True
            continue
        if previous_blank and stripped.isdigit():
            count += 1
        previous_blank = False
    return count


def summarize_failure(text: str) -> str:
    """从日志里挑一行能说明问题的文字，作为失败摘要。

    VideoCaptioner 的日志前面是参数回显与进度框，直接取首行等于什么都没说。
    选取顺序：带 `✗ Error:` 的行 → 其余 `✗` 开头的行 → 第一条非空行。
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for marker in _ERROR_MARKERS:
        for line in lines:
            if marker in line:
                return line.lstrip("✗ ").strip()
    return lines[0] if lines else ""


def probe_environment(*, refresh: bool = False) -> dict:
    """探测字幕提取功能的运行环境。

    实现委托给 subtitle_env（那边管 VideoCaptioner 的跨平台探测与安装指引），
    这里保留同名函数是为了与 scene_runner.probe_environment 对称 —— 路由层
    从「执行层」取环境信息，两个功能的调用点写法一致。
    """
    from app.services.subtitle_env import probe_environment as _probe

    return _probe(refresh=refresh)


# --------------------------------------------------------------------------
# 队列认领（DB 即队列：pending 行就是待执行任务）
# --------------------------------------------------------------------------


def claim_next_pending_id(session_factory: sessionmaker = SessionLocal) -> Optional[int]:
    """取下一个待执行任务的 ID（只读不改状态，真正抢锁在 run_job 里）。"""
    with session_factory() as db:
        row = db.execute(
            select(SubtitleJob.id)
            .where(SubtitleJob.status == SubtitleJobStatus.PENDING)
            .order_by(SubtitleJob.id.asc())
            .limit(1)
        ).first()
        return int(row[0]) if row else None


def recover_interrupted_jobs(
    session_factory: sessionmaker = SessionLocal,
    *,
    stop_grace_seconds: Optional[float] = None,
) -> int:
    """启动时回收上次异常退出留下的 running 任务。

    - 有 child_pid 的：先核对身份再整组杀掉（防 PID 复用误杀）；
    - 任务标记 failed，running 条目标记 failed，pending 条目标记 skipped；
    - pending 任务不动 —— DB 队列会自动接手，不需要恢复代码。

    Returns:
        回收的任务数。
    """
    grace = (
        stop_grace_seconds
        if stop_grace_seconds is not None
        else settings.SUBTITLE_JOB_STOP_GRACE_SECONDS
    )
    recovered = 0

    with session_factory() as db:
        try:
            with transaction(db):
                jobs = list(
                    db.execute(
                        select(SubtitleJob).where(
                            SubtitleJob.status == SubtitleJobStatus.RUNNING
                        )
                    )
                    .scalars()
                    .all()
                )
                for job in jobs:
                    if job.child_pid and is_our_child(job.child_pid):
                        logger.warning(
                            "回收残留的转写进程组 | job=%s | pid=%s", job.id, job.child_pid
                        )
                        terminate_process_group(job.child_pid, grace)
                    job.status = SubtitleJobStatus.FAILED
                    job.error_message = "服务重启，任务中断"
                    job.finished_at = utcnow()
                    job.child_pid = None
                    for item in job.items:
                        if item.status == SubtitleJobItemStatus.RUNNING:
                            item.status = SubtitleJobItemStatus.FAILED
                            item.error_message = "服务重启，任务中断"
                            item.finished_at = utcnow()
                        elif item.status == SubtitleJobItemStatus.PENDING:
                            item.status = SubtitleJobItemStatus.SKIPPED
                    recovered += 1
                db.flush()
        except Exception:  # noqa: BLE001 - 启动恢复绝不能阻断服务启动
            logger.exception("回收中断任务失败")
            return 0

    if recovered:
        logger.info("已回收中断的字幕提取任务 | 数量=%s", recovered)
    return recovered


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------


class SubtitleRunner:
    """字幕提取执行器：逐条视频起 VideoCaptioner 子进程。

    所有外部依赖都可注入，测试传 FakePopen + tick_seconds=0 即可在主线程
    同步跑完，不需要线程也不需要真视频。
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker = SessionLocal,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        launcher: Optional[List[str]] = None,
        tick_seconds: Optional[float] = None,
        progress_seconds: Optional[float] = None,
        video_timeout_seconds: Optional[int] = None,
        stop_grace_seconds: Optional[float] = None,
    ) -> None:
        """初始化执行器。

        Args:
            session_factory: 数据库会话工厂（执行线程自己开会话，与请求线程隔离）。
            popen_factory: 子进程工厂，测试注入 FakePopen。
            launcher: 调用前缀；默认每次执行时探测（探测有缓存，不贵）。
            tick_seconds: 等待子进程时的轮询间隔。
            progress_seconds: 进度字段落库的最小间隔。
            video_timeout_seconds: 单条视频硬超时。
            stop_grace_seconds: 杀进程组的宽限秒数。
        """
        self.session_factory = session_factory
        self.popen_factory = popen_factory
        self._launcher = launcher
        self.tick_seconds = (
            tick_seconds if tick_seconds is not None else settings.SUBTITLE_JOB_TICK_SECONDS
        )
        self.progress_seconds = (
            progress_seconds
            if progress_seconds is not None
            else settings.SUBTITLE_JOB_PROGRESS_SECONDS
        )
        self.video_timeout_seconds = (
            video_timeout_seconds
            if video_timeout_seconds is not None
            else settings.SUBTITLE_JOB_VIDEO_TIMEOUT_SECONDS
        )
        self.stop_grace_seconds = (
            stop_grace_seconds
            if stop_grace_seconds is not None
            else settings.SUBTITLE_JOB_STOP_GRACE_SECONDS
        )

    @property
    def launcher(self) -> Optional[List[str]]:
        """当前可用的调用前缀；没装 VideoCaptioner 时为 None。"""
        if self._launcher is not None:
            return self._launcher
        install = detect()
        return list(install.launcher) if install.installed else None

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
            job = db.get(SubtitleJob, job_id)
            assert job is not None  # _claim 成功说明存在
            logger.info(
                "开始执行字幕提取任务 | id=%s | 视频数=%s | 引擎=%s",
                job.id,
                job.total_videos,
                (job.params or {}).get("asr"),
            )

            launcher = self.launcher
            if launcher is None:
                # 创建任务时校验过，但排队期间用户可能把 VideoCaptioner 卸了。
                # 整条任务直接判失败，别让每条视频各失败一次同样的原因。
                self._fail_all(db, job, "未探测到可用的 VideoCaptioner，无法转写")
                # _fail_all 只标了条目，任务自己的状态还要 _finalize 来落 ——
                # 否则任务会永远停在 running。
                self._finalize(db, job_id)
                return True

            for item in job.items:
                if item.status != SubtitleJobItemStatus.PENDING:
                    continue

                # 取消 / 服务停止：当前条及后续全部标记 skipped，保留已产出的字幕
                if self._is_cancelled(db, job_id) or (
                    stop_event is not None and stop_event.is_set()
                ):
                    self._skip_item(db, item, reason="任务已取消")
                    continue

                self._run_item(db, job, item, launcher, stop_event)

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
                    update(SubtitleJob)
                    .where(
                        SubtitleJob.id == job_id,
                        SubtitleJob.status == SubtitleJobStatus.PENDING,
                    )
                    .values(status=SubtitleJobStatus.RUNNING, started_at=utcnow())
                )
                return result.rowcount == 1
        except Exception:  # noqa: BLE001 - 抢锁失败只说明这条不归我们
            logger.exception("认领字幕提取任务失败 | id=%s", job_id)
            return False

    def _is_cancelled(self, db: Session, job_id: int) -> bool:
        """从库里读最新状态（列查询每次都真实执行 SQL，不受会话缓存影响）。"""
        status = db.execute(
            select(SubtitleJob.status).where(SubtitleJob.id == job_id)
        ).scalar_one_or_none()
        return status == SubtitleJobStatus.CANCELLED

    # ------------------------------------------------------------------
    # 单条视频
    # ------------------------------------------------------------------

    def _run_item(
        self, db: Session, job: SubtitleJob, item: SubtitleJobItem, launcher: List[str], stop_event
    ) -> None:
        """处理一条视频：起子进程 → 盯进度 → 按实际产物定结果。"""
        started_monotonic = time.monotonic()
        output_path = Path(item.output_path)
        out_dir = output_path.parent
        log_path = out_dir / f"{output_path.stem}.{VC_LOG_NAME}"

        item.status = SubtitleJobItemStatus.RUNNING
        item.started_at = utcnow()
        item.duration_seconds = probe_duration(Path(item.source_path))
        job.current_index = item.index
        job.current_video = item.source_name
        # 计时从零开始：这个字段说的是「当前这条跑了多久」，换了视频就得清零
        job.current_elapsed_seconds = 0
        db.commit()

        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._finish_item(
                db, job, item,
                status=SubtitleJobItemStatus.FAILED,
                started_monotonic=started_monotonic,
                exit_code=None,
                error=f"创建输出目录失败：{out_dir}（{exc}）",
            )
            return

        argv = build_argv(launcher, Path(item.source_path), out_dir, dict(job.params or {}))
        logger.info(
            "开始转写 | job=%s | item=%s | %s", job.id, item.index, item.source_name
        )

        process = None
        log_handle = None
        try:
            # stdio 落文件而不是 PIPE：无人读取的管道会把持续输出的子进程憋死
            log_handle = log_path.open("wb")
            process = self.popen_factory(
                argv,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd=str(out_dir),
                env=child_env(),
            )
            self._set_child_pid(db, job.id, process.pid)

            exit_code, aborted = self._wait_for_exit(
                db, job, item, process, started_monotonic, stop_event
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不能中断整个批次
            logger.exception("转写出现异常 | job=%s | item=%s", job.id, item.index)
            if process is not None and process.poll() is None:
                terminate_process_group(process.pid, self.stop_grace_seconds)
            self._finish_item(
                db, job, item,
                status=SubtitleJobItemStatus.FAILED,
                started_monotonic=started_monotonic,
                exit_code=None,
                error=f"转写异常：{exc}",
            )
            return
        finally:
            if log_handle is not None:
                try:
                    log_handle.close()
                except OSError:
                    pass

        if aborted == "cancelled":
            # 取消时字幕可能已经写好了一部分，如实告知而不是一句笼统的「已跳过」
            exists = output_path.is_file() and output_path.stat().st_size > 0
            reason = (
                "任务已取消"
                if not exists
                else "任务已取消，但字幕已生成（已保留在输出目录）"
            )
            self._skip_item(db, item, reason=reason, started_monotonic=started_monotonic)
            self._refresh_job_progress(db, job)
            return
        if aborted == "stopped":
            self._finish_item(
                db, job, item,
                status=SubtitleJobItemStatus.FAILED,
                started_monotonic=started_monotonic,
                exit_code=exit_code,
                error="服务停止，任务中断",
            )
            return
        if aborted == "timeout":
            self._finish_item(
                db, job, item,
                status=SubtitleJobItemStatus.FAILED,
                started_monotonic=started_monotonic,
                exit_code=exit_code,
                error=f"转写超时（超过 {self.video_timeout_seconds} 秒），已终止",
            )
            return

        self._collect_result(
            db, job, item, output_path, log_path, exit_code, started_monotonic
        )

    def _wait_for_exit(self, db, job, item, process, started_monotonic, stop_event):
        """盯着子进程直到退出 / 被取消 / 被停止 / 超时。

        Returns:
            (exit_code, aborted)：aborted 为 None / "cancelled" / "stopped" / "timeout"。
        """
        last_progress_write = 0.0

        while True:
            exit_code = process.poll()
            if exit_code is not None:
                return exit_code, None

            # 取消：整组杀掉，已写好的字幕保留
            if self._is_cancelled(db, job.id):
                logger.info("任务被取消，终止当前子进程 | job=%s | pid=%s", job.id, process.pid)
                terminate_process_group(process.pid, self.stop_grace_seconds)
                return process.poll(), "cancelled"

            # 服务停止：同样整组杀掉，但这条算 failed 而不是 skipped
            if stop_event is not None and stop_event.is_set():
                logger.info("服务停止，终止当前子进程 | job=%s | pid=%s", job.id, process.pid)
                terminate_process_group(process.pid, self.stop_grace_seconds)
                return process.poll(), "stopped"

            # 单条硬超时：防止一条卡住的转写堵死整个队列
            if time.monotonic() - started_monotonic > self.video_timeout_seconds:
                logger.error(
                    "转写超时，强制终止 | job=%s | item=%s | pid=%s",
                    job.id, item.index, process.pid,
                )
                terminate_process_group(process.pid, self.stop_grace_seconds)
                return process.poll(), "timeout"

            now = time.monotonic()
            if now - last_progress_write >= self.progress_seconds:
                last_progress_write = now
                self._refresh_job_progress(db, job, started_monotonic=started_monotonic)

            time.sleep(self.tick_seconds)

    def _collect_result(
        self, db, job, item, output_path: Path, log_path: Path, exit_code, started_monotonic
    ) -> None:
        """按实际产物 + 退出码判定一条视频的结果。

        判成功的依据是「目标 .srt 存在且非空」，不是退出码 —— 见模块头部的
        说明 b：VideoCaptioner 可能在收尾阶段报错而字幕其实已经写好，也可能
        退出码 0 却什么都没产出。两种边界都要如实反映。
        """
        try:
            size = output_path.stat().st_size
        except OSError:
            size = 0

        item.file_size = size
        item.subtitle_exists = size > 0
        item.segment_count = count_segments(output_path) if size > 0 else 0

        if item.subtitle_exists:
            if exit_code == 0:
                self._finish_item(
                    db, job, item,
                    status=SubtitleJobItemStatus.SUCCESS,
                    started_monotonic=started_monotonic,
                    exit_code=exit_code,
                    error="",
                )
            else:
                # 产物在、退出码非 0：认成功，但把原因记下来（用户在详情里能看到）
                tail = read_log_tail(log_path)
                summary = summarize_failure(tail)
                self._finish_item(
                    db, job, item,
                    status=SubtitleJobItemStatus.SUCCESS,
                    started_monotonic=started_monotonic,
                    exit_code=exit_code,
                    error=(
                        f"字幕已生成，但命令以退出码 {exit_code} 结束"
                        + (f"：{summary}" if summary else "")
                    ),
                )
            return

        tail = read_log_tail(log_path)
        summary = summarize_failure(tail)
        self._finish_item(
            db, job, item,
            status=SubtitleJobItemStatus.FAILED,
            started_monotonic=started_monotonic,
            exit_code=exit_code,
            error=(
                f"{summary}\n\n{tail}"
                if summary
                else (tail or f"退出码 {exit_code}，未产出字幕文件")
            ),
        )

    def _fail_all(self, db: Session, job: SubtitleJob, reason: str) -> None:
        """把任务里所有未完成的条目标记为失败（整条任务级的前置条件不满足）。"""
        for item in job.items:
            if item.status != SubtitleJobItemStatus.PENDING:
                continue
            item.status = SubtitleJobItemStatus.FAILED
            item.error_message = reason
            item.finished_at = utcnow()
        job.error_message = reason
        db.commit()

    # ------------------------------------------------------------------
    # 落库小工具
    # ------------------------------------------------------------------

    def _refresh_job_progress(
        self, db: Session, job: SubtitleJob, *, started_monotonic: Optional[float] = None
    ) -> None:
        """把「当前这条跑了多久」写进任务字段（供轮询接口直接读）。

        只更新这一个字段：转写没有任何可解析的进度输出（见模块头部说明 a），
        已用时间是唯一能如实给出的实时数字。产物数量由 item 表承载，不在这里
        重复统计 —— 任务级的 subtitle_count 在每条收尾时累加。
        """
        if started_monotonic is not None:
            job.current_elapsed_seconds = int(round(time.monotonic() - started_monotonic))
        db.commit()

    @staticmethod
    def _clear_current_progress(job: SubtitleJob) -> None:
        """任务结束时抹掉「当前视频」那一组字段。

        不抹的话，页面上会留着一条永远停在「转写中 03:21」的实时进度，
        而任务其实早就结束了。前端只在 running 时渲染这组字段，但接口数据
        本身也该是自洽的：任务不在跑了，就没有「当前视频」。
        """
        job.current_video = ""
        job.current_elapsed_seconds = 0

    def _set_child_pid(self, db: Session, job_id: int, pid: Optional[int]) -> None:
        """更新当前子进程句柄（取消、超时、孤儿回收共用的唯一依据）。"""
        db.execute(update(SubtitleJob).where(SubtitleJob.id == job_id).values(child_pid=pid))
        db.commit()

    def _finish_item(
        self, db, job, item, *, status, started_monotonic, exit_code, error
    ) -> None:
        """收尾一条视频：写结果、累计任务级计数。"""
        item.status = status
        item.exit_code = exit_code
        item.error_message = error[:4000] if error else ""
        item.elapsed_seconds = int(round(time.monotonic() - started_monotonic))
        item.finished_at = utcnow()

        job.completed_videos += 1
        if status == SubtitleJobItemStatus.FAILED:
            job.failed_videos += 1
        if item.subtitle_exists:
            job.subtitle_count += 1
        job.child_pid = None
        db.commit()

        logger.info(
            "转写完成 | job=%s | item=%s | 状态=%s | 字幕=%s 条 | 耗时=%ss",
            job.id, item.index, status, item.segment_count, item.elapsed_seconds,
        )

    def _skip_item(self, db, item, *, reason, started_monotonic=None) -> None:
        """把一条未执行/被打断的视频标记为 skipped。"""
        item.status = SubtitleJobItemStatus.SKIPPED
        item.error_message = reason
        if started_monotonic is not None:
            item.elapsed_seconds = int(round(time.monotonic() - started_monotonic))
        item.finished_at = utcnow()
        db.commit()

    def _finalize(self, db: Session, job_id: int) -> None:
        """汇总任务终态：取消保持 cancelled，否则按条目结果定 success/partial/failed。"""
        # populate_existing：取消是在另一个会话里提交的，这里必须读最新状态，
        # 否则会把 cancelled 覆盖成 failed
        job = db.get(SubtitleJob, job_id, populate_existing=True)
        if job is None:
            return

        if job.status == SubtitleJobStatus.CANCELLED:
            job.skipped_videos = sum(
                1 for item in job.items if item.status == SubtitleJobItemStatus.SKIPPED
            )
            job.child_pid = None
            job.subtitle_count = sum(1 for item in job.items if item.subtitle_exists)
            self._clear_current_progress(job)
            db.commit()
            logger.info("字幕提取任务已取消 | id=%s", job_id)
            return

        failed = sum(1 for item in job.items if item.status == SubtitleJobItemStatus.FAILED)
        skipped = sum(1 for item in job.items if item.status == SubtitleJobItemStatus.SKIPPED)
        succeeded = job.total_videos - failed - skipped

        if failed == 0 and skipped == 0:
            job.status = SubtitleJobStatus.SUCCESS
        elif succeeded > 0:
            job.status = SubtitleJobStatus.PARTIAL
        else:
            job.status = SubtitleJobStatus.FAILED

        job.failed_videos = failed
        job.skipped_videos = skipped
        job.subtitle_count = sum(1 for item in job.items if item.subtitle_exists)
        if failed:
            reasons = [
                f"{item.source_name}：{item.error_message.splitlines()[0] if item.error_message else '失败'}"
                for item in job.items
                if item.status == SubtitleJobItemStatus.FAILED
            ][:3]
            job.error_message = "；".join(reasons)
        job.child_pid = None
        self._clear_current_progress(job)
        job.finished_at = utcnow()
        db.commit()

        logger.info(
            "字幕提取任务结束 | id=%s | 状态=%s | 成功=%s | 失败=%s | 字幕=%s 份",
            job.id, job.status, succeeded, failed, job.subtitle_count,
        )
