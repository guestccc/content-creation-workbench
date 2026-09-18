"""镜头分割任务的执行层：起 vct 子进程、盯进度、收尾。

设计要点（与 scene_job_service.py 的职责划分）：
- service 管状态机与查询；本模块管「真正把一条视频切出来」；
- 本模块不依赖 FastAPI，所有外部依赖（会话工厂、Popen、时钟参数）都可注入，
  测试可以传 FakePopen 在主线程同步跑完整个流程，不起线程、不起真子进程。

子进程管理的三条硬规则：
1. stdio 必须落文件，绝不 PIPE —— vct/ffmpeg 持续输出，无人读取的管道
   缓冲写满后子进程会永久阻塞在 write 上（任务「跑到一半不动了」）；
2. 必须 start_new_session=True 并整组 kill —— vct 自己会拉起 ffmpeg，
   只杀 vct 会留下孤儿 ffmpeg 继续啃 CPU；
3. 取消/超时/服务停止共用同一个终止函数，且等待必须有界 —— uvicorn
   reloader 的 join() 没有超时，这里任何一次无界等待都会让热重载卡死。
"""

import csv
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.materials import CLIPS, SOURCE, materials_root, subdir
from app.core.logging import get_logger
from app.db.session import SessionLocal, transaction
from app.models.content import utcnow

# 子进程工具函数与混剪共用，实现移到了 media_tools；这里原样再导出，
# 是为了让本模块的既有调用方（含 tests/test_scene_runner.py）不必改 import。
from app.services.media_tools import (  # noqa: F401  (重导出)
    CHILD_CREATION_FLAGS,
    child_env,
    command_line,
    find_tool,
    generate_thumbnail,
    probe_duration,
    read_log_tail,
    terminate_process_group,
)
from app.models.scene_job import (
    SceneJob,
    SceneJobItem,
    SceneJobItemStatus,
    SceneJobMode,
    SceneJobStatus,
)

logger = get_logger(__name__)

# 子进程日志名：写在每条视频的输出目录里，失败时读它的尾部给用户看真实报错
VCT_LOG_NAME = "vct.log"

# 片段文件名模式（与 vct CLI 的产物约定一致：<视频名>_clip_NNN.mp4）
CLIP_GLOB = "*_clip_*.mp4"

# vct 的机器可读进度标记，形如 `#vct-progress split 3 38`。
# 契约定义在 vctl/ui.py 的 PROGRESS_PREFIX / progress() —— 那边改格式，
# 这里必须一起改，测试两头都钉着。
#
# 为什么不解析人类可读的输出（`[2/2] 切割 38 个片段`）：那是给人看的文案，
# 措辞一变解析就悄悄失效；单镜头视频压根不打印那行，猜都无从猜起。
PROGRESS_MARKER = re.compile(
    r"^#vct-progress[ \t]+(\S+)[ \t]+(\d+)[ \t]+(\d+)[ \t]*$", re.MULTILINE
)

#: 阶段名，与 vctl/ui.py 的 PHASE_DETECT / PHASE_SPLIT 一一对应。
PROGRESS_PHASE_DETECT = "detect"
PROGRESS_PHASE_SPLIT = "split"


# --------------------------------------------------------------------------
# 纯函数：argv 构造、环境、进程组管理（全部可独立测试）
# --------------------------------------------------------------------------


def vct_launcher(vct_path: str) -> List[str]:
    """给出起 vct 子进程的 argv 前缀（平台相关的唯一分岔口）。

    POSIX：直接 exec vct 脚本，`#!/usr/bin/env bash` 由内核处理 —— [vct_path]。

    Windows：CreateProcess 执行不了无扩展名的脚本（shebang 只是文本，硬起
    就是 WinError 193「%1 不是有效的 Win32 应用程序」），所以绕开脚本、
    用后端自己的解释器跑模块：vctl 只用标准库，后端的解释器必然 ≥3.10。
    vct 脚本里「找解释器」的那段工作在这里顶掉，「设 PYTHONPATH /
    禁写 pyc」的那段在 scene_child_env() 里顶掉。
    """
    if os.name == "nt":
        return [sys.executable, "-u", "-m", "vctl"]
    return [vct_path]


def scene_child_env() -> Dict[str, str]:
    """child_env() 的镜头分割封装：Windows 上额外注入 vctl 的 import 路径。

    Windows 走 `python -m vctl`（见 vct_launcher），vctl 包躺在工具箱根
    目录下而不在 site-packages 里，必须把工具箱根目录塞进 PYTHONPATH ——
    这正是 vct 脚本里 `export PYTHONPATH="${HERE}..."` 那一行。已有的
    PYTHONPATH 保留（可能指向用户自己的开发目录）。PYTHONDONTWRITEBYTECODE
    同脚本原意：免得工具箱目录散落 __pycache__。

    POSIX 不注这些 —— vct 脚本自己会设，这里重复只会留下两份要同步的真相。
    """
    env = child_env()
    if os.name != "nt":
        return env
    toolbox_root = str(Path(settings.SCENE_VCT_PATH).resolve().parent)
    existing = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if toolbox_root not in existing:
        existing.insert(0, toolbox_root)
    env["PYTHONPATH"] = os.pathsep.join(existing)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Windows 的 stdio 默认编码随控制台代码页（中文系统是 GBK）：vct 的中文
    # 日志落进 vct.log 就是 GBK 字节，read_log_tail 按 UTF-8 读回来全是乱码，
    # 失败原因就白记了。PYTHONIOENCODING 只影响 stdio 三个流，不碰文件默认编码。
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def build_argv(launcher: List[str], video: Path, out_dir: Path, params: dict, *, split: bool) -> List[str]:
    """把白名单参数映射成 vct scene 的 argv。

    只认 params 里的四个白名单键（detector/threshold/min_len/copy），
    其余键直接忽略 —— params 来自数据库，不信任其中的额外内容。

    Args:
        launcher: 进程启动前缀（vct_launcher 的产物）：POSIX 是脚本路径
            本身，Windows 是 [解释器, -u, -m, vctl]。
        video: 输入视频（必须是绝对路径，以服务名开头的 `-` 路径已被 schema 层挡掉）。
        out_dir: 输出目录。
        params: resolve_params 的产物。
        split: True 切出片段，False 只检测导出 CSV。
    """
    argv = [*launcher, "scene", str(video), "-o", str(out_dir), "--csv"]

    if split:
        argv.append("--split")

    detector = params.get("detector")
    if detector:
        argv += ["--detector", str(detector)]

    threshold = params.get("threshold")
    if threshold is not None:
        argv += ["--threshold", str(threshold)]

    min_len = params.get("min_len")
    if min_len is not None:
        argv += ["--min-len", str(min_len)]

    if params.get("copy"):
        argv.append("--copy")

    return argv


def is_our_child(pid: int, vct_bin: str) -> bool:
    """校验一个 PID 现在确实还是我们起的 vct 进程。

    机器重启后 PID 会被复用，孤儿回收时盲杀可能干掉无辜进程
    （比如用户刚打开的编辑器），所以回收前必须用命令行核对身份。
    """
    if pid <= 0:
        return False
    command = command_line(pid)
    # vct 入口是 bash 脚本，命令行里必然包含 vct 的路径或 vctl 模块名
    return vct_bin in command or "vctl" in command


def parse_scenes_csv(csv_path: Path) -> List[dict]:
    """解析 vct 导出的切点 CSV。

    CSV 在 _split_scenes 返回后一次性写入（不是增量），所以只在子进程
    退出后读取。表头是中文：序号,开始(秒),结束(秒),时长(秒),开始时间码,结束时间码。

    Returns:
        [{"number": 1, "start": 0.0, "end": 4.2, "duration": 4.2}, ...]；
        文件不存在或解析失败时返回空列表（调用方据此降级，不崩溃）。
    """
    scenes: List[dict] = []
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            rows = list(reader)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("读取切点 CSV 失败 | %s | %s", csv_path, exc)
        return scenes

    for row in rows[1:]:  # 跳过表头
        if len(row) < 4:
            continue
        try:
            scenes.append(
                {
                    "number": int(row[0]),
                    "start": float(row[1]),
                    "end": float(row[2]),
                    "duration": float(row[3]),
                }
            )
        except ValueError:
            continue
    return scenes


def parse_progress(text: str) -> Optional[Dict[str, object]]:
    """从 vct 的输出里解析出最新一条进度标记。

    取**最后一条**而不是第一条：标记是单调前进的，日志尾部那行才是当前
    进度。调用方喂进来的通常已经是日志尾部（read_log_tail），日志中途
    没被截断时也不会退化 —— 取最后一条永远是对的。

    Args:
        text: vct 的输出文本（可含无关内容）。

    Returns:
        {"phase": "detect" | "split", "done": int, "total": int}；
        没有标记行时返回 None（老版本 vct 就是这种情况，调用方据此降级）。
    """
    matches = PROGRESS_MARKER.findall(text)
    if not matches:
        return None
    phase, done, total = matches[-1]
    return {"phase": phase, "done": int(done), "total": int(total)}


# 纯装饰行（分隔线、空框线）判定用到的字符集
_DECORATION_CHARS = set("═─━—-=*_· \t")


def summarize_failure(text: str) -> str:
    """从 vct 输出里挑出一行能说明问题的文字，作为失败摘要。

    vct 的日志以 `════` 边框和参数回显开头，直接取首行只会得到一串分隔线，
    在界面上等于什么都没说。选取顺序：
      1. 含 `Error:` 的行 —— 底层工具（scenedetect / ffmpeg）的真实病因；
      2. vct 自己的 `✗ ...` 失败行；
      3. 第一行非空、非装饰的文字。
    """
    lines = [line.strip() for line in text.splitlines()]
    for line in lines:
        if "Error:" in line:
            return line
    for line in reversed(lines):
        if line.startswith("✗"):
            return line.lstrip("✗ ").strip()
    for line in lines:
        if line and set(line) > _DECORATION_CHARS:
            return line
    return ""


def probe_environment() -> dict:
    """探测镜头分割功能的运行环境，结果直接给「依赖自检」接口用。"""
    vct_path = settings.SCENE_VCT_PATH
    vct_exists = os.path.isfile(vct_path) and os.access(vct_path, os.X_OK)

    scenedetect = find_tool("scenedetect")
    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")

    dependencies = [
        {
            "name": "vct",
            "ok": vct_exists,
            "path": vct_path,
            "detail": "镜头分割命令行工具" if vct_exists else "路径不存在或没有执行权限",
            "fix_hint": "" if vct_exists else "确认 SCENE_VCT_PATH 指向 内容制作工具/vct",
        },
        {
            "name": "scenedetect",
            "ok": scenedetect is not None,
            "path": scenedetect or "",
            "detail": "镜头检测引擎" if scenedetect else "未在 PATH 中找到",
            "fix_hint": "" if scenedetect else "pip install scenedetect 或放入 ~/.local/bin",
        },
        {
            "name": "ffmpeg",
            "ok": ffmpeg is not None,
            "path": ffmpeg or "",
            "detail": "视频切割与缩略图抽帧" if ffmpeg else "未在 PATH 中找到",
            "fix_hint": "" if ffmpeg else "brew install ffmpeg 或放入 ~/.local/bin",
        },
        {
            "name": "ffprobe",
            "ok": ffprobe is not None,
            "path": ffprobe or "",
            "detail": "视频时长探测" if ffprobe else "未找到（时长列将留空，不影响切割）",
            "fix_hint": "" if ffprobe else "随 ffmpeg 一起安装",
        },
    ]
    return {
        "ready": vct_exists and scenedetect is not None and ffmpeg is not None,
        "vct_path": vct_path,
        "vct_exists": vct_exists,
        # 素材目录不是「依赖」，但它得跟自检结果一起回给前端：
        # 页面打开时输入/输出目录要默认停在 source/ 与 clips/，
        # 顺路带回省两次请求。目录规划见 app/core/materials.py。
        "materials_dir": str(materials_root()),
        "default_input_dir": str(subdir(SOURCE)),
        "default_output_dir": str(subdir(CLIPS)),
        "dependencies": dependencies,
    }


# --------------------------------------------------------------------------
# 队列认领（DB 即队列：pending 行就是待执行任务）
# --------------------------------------------------------------------------


def claim_next_pending_id(session_factory: sessionmaker = SessionLocal) -> Optional[int]:
    """取下一个待执行任务的 ID（只读不改状态，真正抢锁在 run_job 里）。

    之所以拆开：run_job 的认领是一次条件 UPDATE（pending→running），
    天然防重；这里即使与取消操作并发，run_job 抢锁失败也会干净地返回。
    """
    with session_factory() as db:
        row = db.execute(
            select(SceneJob.id)
            .where(SceneJob.status == SceneJobStatus.PENDING)
            .order_by(SceneJob.id.asc())
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
        else settings.SCENE_JOB_STOP_GRACE_SECONDS
    )
    vct_bin = settings.SCENE_VCT_PATH
    recovered = 0

    with session_factory() as db:
        try:
            with transaction(db):
                jobs = list(
                    db.execute(
                        select(SceneJob).where(SceneJob.status == SceneJobStatus.RUNNING)
                    )
                    .scalars()
                    .all()
                )
                for job in jobs:
                    if job.child_pid and is_our_child(job.child_pid, vct_bin):
                        logger.warning(
                            "回收残留的 vct 进程组 | job=%s | pid=%s", job.id, job.child_pid
                        )
                        terminate_process_group(job.child_pid, grace)
                    job.status = SceneJobStatus.FAILED
                    job.error_message = "服务重启，任务中断"
                    job.finished_at = utcnow()
                    job.child_pid = None
                    for item in job.items:
                        if item.status == SceneJobItemStatus.RUNNING:
                            item.status = SceneJobItemStatus.FAILED
                            item.error_message = "服务重启，任务中断"
                            item.finished_at = utcnow()
                        elif item.status == SceneJobItemStatus.PENDING:
                            item.status = SceneJobItemStatus.SKIPPED
                    recovered += 1
                db.flush()
        except Exception:  # noqa: BLE001 - 启动恢复绝不能阻断服务启动
            logger.exception("回收中断任务失败")
            return 0

    if recovered:
        logger.info("已回收中断的镜头分割任务 | 数量=%s", recovered)
    return recovered


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------


class SceneRunner:
    """镜头分割执行器：逐条视频起 vct 子进程并把进度落库。

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
            launcher: vct 子进程的 argv 前缀，默认 vct_launcher(settings.SCENE_VCT_PATH)
                （按平台分岔）。测试注入定值，让断言不随平台漂移。
            tick_seconds: 等待子进程时的轮询间隔。
            progress_seconds: 进度字段落库的最小间隔。
            video_timeout_seconds: 单视频硬超时。
            stop_grace_seconds: 杀进程组的宽限秒数。
        """
        self.session_factory = session_factory
        self.popen_factory = popen_factory
        self.launcher = list(launcher) if launcher is not None else vct_launcher(settings.SCENE_VCT_PATH)
        self.tick_seconds = (
            tick_seconds if tick_seconds is not None else settings.SCENE_JOB_TICK_SECONDS
        )
        self.progress_seconds = (
            progress_seconds
            if progress_seconds is not None
            else settings.SCENE_JOB_PROGRESS_SECONDS
        )
        self.video_timeout_seconds = (
            video_timeout_seconds
            if video_timeout_seconds is not None
            else settings.SCENE_JOB_VIDEO_TIMEOUT_SECONDS
        )
        self.stop_grace_seconds = (
            stop_grace_seconds
            if stop_grace_seconds is not None
            else settings.SCENE_JOB_STOP_GRACE_SECONDS
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
            True 表示执行了该任务；False 表示任务已不存在或已被取消/抢占。
        """
        with self.session_factory() as db:
            if not self._claim(db, job_id):
                return False

        with self.session_factory() as db:
            job = db.get(SceneJob, job_id)
            assert job is not None  # _claim 成功说明存在
            logger.info(
                "开始执行镜头分割任务 | id=%s | mode=%s | 视频数=%s",
                job.id,
                job.mode,
                job.total_videos,
            )

            for item in job.items:
                if item.status != SceneJobItemStatus.PENDING:
                    continue

                # 取消 / 服务停止：后续条目标记 skipped，保留已完成的结果。
                # 两种原因必须分开写 —— 服务停止时标成「任务已取消」，用户会以为
                # 谁点了取消，对真正的中断原因（如热重载）毫无头绪。
                # _run_item 内部若撞上取消会把当前条目标记好，这里兜住后续的条目。
                if self._is_cancelled(db, job_id):
                    self._skip_item(db, item, reason="任务已取消")
                    continue
                if stop_event is not None and stop_event.is_set():
                    self._skip_item(db, item, reason="服务停止或重启，未执行")
                    continue

                self._run_item(db, job, item, stop_event)

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
                    update(SceneJob)
                    .where(
                        SceneJob.id == job_id,
                        SceneJob.status == SceneJobStatus.PENDING,
                    )
                    .values(
                        status=SceneJobStatus.RUNNING,
                        started_at=utcnow(),
                    )
                )
                return result.rowcount == 1
        except Exception:  # noqa: BLE001 - 抢锁失败只说明这条不归我们
            logger.exception("认领镜头分割任务失败 | id=%s", job_id)
            return False

    def _is_cancelled(self, db: Session, job_id: int) -> bool:
        """从库里读最新状态（列查询每次都真实执行 SQL，不受会话缓存影响）。"""
        status = db.execute(
            select(SceneJob.status).where(SceneJob.id == job_id)
        ).scalar_one_or_none()
        return status == SceneJobStatus.CANCELLED

    # ------------------------------------------------------------------
    # 单条视频
    # ------------------------------------------------------------------

    def _run_item(self, db: Session, job: SceneJob, item: SceneJobItem, stop_event) -> None:
        """处理一条视频：起子进程 → 盯进度 → 按退出码和实际产物定结果。"""
        started_monotonic = time.monotonic()
        split = job.mode == SceneJobMode.SPLIT
        out_dir = Path(item.output_dir)
        log_path = out_dir / VCT_LOG_NAME

        item.status = SceneJobItemStatus.RUNNING
        item.started_at = utcnow()
        item.duration_seconds = probe_duration(Path(item.source_path))
        job.current_index = item.index
        job.current_video = item.source_name
        # 进度字段一律清零：这几列说的是「当前这条」的进度，换了视频就得
        # 从头算。不清的话新视频会顶着上一条的「38/38」开场，看起来像已经
        # 切完了，直到第一次轮询（默认 2 秒后）才被纠正。
        job.current_clips = 0
        job.current_clip_names = []
        job.current_phase = ""
        job.current_total_clips = 0
        db.commit()

        argv = build_argv(
            self.launcher, Path(item.source_path), out_dir, dict(job.params or {}), split=split
        )
        logger.info("处理视频 | job=%s | item=%s | %s", job.id, item.index, item.source_name)

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
                creationflags=CHILD_CREATION_FLAGS,
                cwd=str(out_dir),
                env=scene_child_env(),
            )
            self._set_child_pid(db, job.id, process.pid)

            exit_code, aborted = self._wait_for_exit(db, job, item, process, started_monotonic, stop_event)
        except Exception as exc:  # noqa: BLE001 - 单条失败不能中断整个批次
            logger.exception(
                "视频处理出现异常 | job=%s | item=%s", job.id, item.index
            )
            if process is not None and process.poll() is None:
                terminate_process_group(process.pid, self.stop_grace_seconds)
            self._finish_item(
                db, job, item,
                status=SceneJobItemStatus.FAILED,
                started_monotonic=started_monotonic,
                exit_code=None,
                error=f"处理异常：{exc}",
            )
            return
        finally:
            if log_handle is not None:
                try:
                    log_handle.close()
                except OSError:
                    pass

        if aborted == "cancelled":
            # 取消时磁盘上可能已有部分片段，如实告知而不是一句笼统的「已跳过」
            partial_clips = len(list(out_dir.glob("*_clip_*.mp4"))) if split else 0
            reason = (
                "任务已取消"
                if partial_clips == 0
                else f"任务已取消，取消前已切出 {partial_clips} 个片段（已保留在输出目录）"
            )
            self._skip_item(db, item, reason=reason, started_monotonic=started_monotonic)
            self._refresh_job_progress(db, job)
            return
        if aborted == "stopped":
            self._finish_item(
                db, job, item,
                status=SceneJobItemStatus.FAILED,
                started_monotonic=started_monotonic,
                exit_code=exit_code,
                # 不是处理本身失败（退出码是kill的产物），把「可以重试」写明
                error="服务停止或重启，任务中断（可直接重新发起）",
            )
            return
        if aborted == "timeout":
            self._finish_item(
                db, job, item,
                status=SceneJobItemStatus.FAILED,
                started_monotonic=started_monotonic,
                exit_code=exit_code,
                error=f"处理超时（超过 {self.video_timeout_seconds} 秒），已终止",
            )
            return

        self._collect_result(db, job, item, out_dir, log_path, exit_code, split, started_monotonic)

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

            # 取消：整组杀掉，保留已完成片段的计数
            if self._is_cancelled(db, job.id):
                logger.info("任务被取消，终止当前子进程 | job=%s | pid=%s", job.id, process.pid)
                terminate_process_group(process.pid, self.stop_grace_seconds)
                return process.poll(), "cancelled"

            # 服务停止：同样整组杀掉，但本条算 failed 而不是 skipped
            if stop_event is not None and stop_event.is_set():
                logger.info("服务停止，终止当前子进程 | job=%s | pid=%s", job.id, process.pid)
                terminate_process_group(process.pid, self.stop_grace_seconds)
                return process.poll(), "stopped"

            # 单视频硬超时：防止一条卡住的 vct 堵死整个队列
            if time.monotonic() - started_monotonic > self.video_timeout_seconds:
                logger.error(
                    "视频处理超时，强制终止 | job=%s | item=%s | pid=%s",
                    job.id, item.index, process.pid,
                )
                terminate_process_group(process.pid, self.stop_grace_seconds)
                return process.poll(), "timeout"

            # 进度：数输出目录里的片段文件。目录是创建任务时预建的空目录，
            # 所以文件个数就等于这条视频的真实进度，不会被历史残留污染。
            now = time.monotonic()
            if now - last_progress_write >= self.progress_seconds:
                last_progress_write = now
                self._refresh_job_progress(db, job)

            time.sleep(self.tick_seconds)

    def _collect_result(self, db, job, item, out_dir, log_path, exit_code, split, started_monotonic):
        """按退出码 + 实际产物判定一条视频的结果。

        两个容易判错的边界（读 vct 源码确认的契约）：
        1. 单镜头视频 + --split → 退出码 0 但零片段，这是合法结果不是失败；
        2. 部分片段切割失败 → 退出码 5，但成功的片段是好的，不能当全败。
        """
        scenes = parse_scenes_csv(out_dir / f"{Path(item.source_path).stem}_scenes.csv")
        clip_names = sorted(p.name for p in out_dir.glob(CLIP_GLOB))
        scene_count = len(scenes)
        clip_count = len(clip_names)

        item.scenes = scenes
        item.scene_count = scene_count
        item.clip_count = clip_count
        item.clip_names = clip_names
        # 单镜头：全片无画面跳变。此时切不出片段是正常结果，界面上要说清楚
        item.single_shot = scene_count <= 1

        if exit_code == 0:
            self._finish_item(
                db, job, item,
                status=SceneJobItemStatus.SUCCESS,
                started_monotonic=started_monotonic,
                exit_code=exit_code,
                error="",
            )
            return

        # 退出码非 0：只要有片段产出就是「部分失败」，如实呈现成功数
        if split and clip_count > 0:
            failed = max(scene_count - clip_count, 0) if scene_count else 0
            item.failed_clip_count = failed
            tail = read_log_tail(log_path)
            self._finish_item(
                db, job, item,
                status=SceneJobItemStatus.SUCCESS,
                started_monotonic=started_monotonic,
                exit_code=exit_code,
                error=(
                    f"部分片段切割失败（成功 {clip_count} 个，失败 {failed} 个）"
                    + (f"\n\n{tail}" if tail else "")
                ),
            )
            return

        tail = read_log_tail(log_path)
        # 摘要放第一行（界面行内展示），完整日志跟在后面（悬停/详情里看）
        summary = summarize_failure(tail)
        self._finish_item(
            db, job, item,
            status=SceneJobItemStatus.FAILED,
            started_monotonic=started_monotonic,
            exit_code=exit_code,
            error=(
                f"{summary}\n\n{tail}"
                if summary
                else (tail or f"vct 退出码 {exit_code}，未产出任何片段")
            ),
        )

    # ------------------------------------------------------------------
    # 落库小工具
    # ------------------------------------------------------------------

    def _refresh_job_progress(self, db: Session, job: SceneJob) -> None:
        """把当前视频的进度写进任务字段（供轮询接口直接读）。

        调用一次就写一次 —— 限流在调用方（`_wait_for_exit` 按
        SCENE_JOB_PROGRESS_SECONDS 决定多久刷一次），这里不做二次判断。

        两个数据来源，各管一段：

        1. **vct 的进度标记**（读 vct.log 尾部）—— 权威来源。它同时给出
           「现在是检测还是切割」「切到第几个 / 共几个」，检测阶段没有分母
           时如实只报阶段，前端据此显示「检测中」而不是一根假进度条。
        2. **输出目录里的片段文件数** —— 兜底。老版本 vct 不发标记，这时
           至少还能靠产物数让用户看到「这条在往前走」。
        """
        out_dir = Path(job.items[job.current_index - 1].output_dir) if job.current_index else None
        if out_dir is None or not out_dir.is_dir():
            db.commit()
            return

        # 产物视角的片段数。整列表重新赋值，触发 SQLAlchemy 的变更检测。
        names = sorted(p.name for p in out_dir.glob(CLIP_GLOB))
        job.current_clip_names = names

        marker = parse_progress(read_log_tail(out_dir / VCT_LOG_NAME))
        if marker is not None:
            job.current_phase = str(marker["phase"])
            if marker["phase"] == PROGRESS_PHASE_SPLIT:
                job.current_total_clips = int(marker["total"])
                # 分子取 vct 上报的 done 而不是文件数：切失败、被跳过的片段
                # 也照样往前走，用户关心的是「还剩多少」。文件数是产物视角，
                # 有片段失败时会卡在最后一个数字上不动，看起来像卡死了。
                # （这一条的成色由 item.clip_count / failed_clip_count 另行如实呈现）
                job.current_clips = int(marker["done"])
            else:
                # 检测阶段的分母是「检测这一步」，不是片段数。原样写进
                # current_total_clips 会让前端把「0/1」渲染成片段进度条，
                # 那是个编出来的分母 —— 留 0，让它按阶段显示「检测中」。
                job.current_total_clips = 0
                job.current_clips = len(names)
        elif not job.current_phase:
            # 一条标记都没读到：老版本 vct 不发这个，退化成数文件（至少还能
            # 看出「这条在往前走」）。已经读过标记的就不回退了 —— 那说明只是
            # 这一眼的日志尾部被截在标记行中间，保持上一次的值，别让数字跳。
            job.current_clips = len(names)

        db.commit()

    @staticmethod
    def _clear_current_progress(job: SceneJob) -> None:
        """任务结束时抹掉「当前视频」那一组字段。

        不抹的话，页面上会留着一条永远停在「检测中」或「38/38」的实时进度，
        而任务其实早就结束了 —— 前端只在 running 时渲染这组字段，但接口
        数据本身也该是自洽的：任务不在跑了，就没有「当前视频」。
        （成色数据在 item.clip_count / elapsed_seconds 里，不受影响）
        """
        job.current_video = ""
        job.current_clips = 0
        job.current_clip_names = []
        job.current_phase = ""
        job.current_total_clips = 0

    def _set_child_pid(self, db: Session, job_id: int, pid: Optional[int]) -> None:
        """更新当前子进程句柄（取消、超时、孤儿回收共用的唯一依据）。"""
        db.execute(update(SceneJob).where(SceneJob.id == job_id).values(child_pid=pid))
        db.commit()

    def _finish_item(self, db, job, item, *, status, started_monotonic, exit_code, error) -> None:
        """收尾一条视频：写结果、累计任务级计数。"""
        item.status = status
        item.exit_code = exit_code
        item.error_message = error[:4000] if error else ""
        item.elapsed_seconds = int(round(time.monotonic() - started_monotonic))
        item.finished_at = utcnow()

        job.completed_videos += 1
        if status == SceneJobItemStatus.FAILED:
            job.failed_videos += 1
        job.clip_count += item.clip_count
        job.scene_count += item.scene_count
        job.child_pid = None
        db.commit()

        logger.info(
            "视频处理完成 | job=%s | item=%s | 状态=%s | 片段=%s | 耗时=%ss",
            job.id, item.index, status, item.clip_count, item.elapsed_seconds,
        )

    def _skip_item(self, db, item, *, reason, started_monotonic=None) -> None:
        """把一条未执行/被打断的视频标记为 skipped。"""
        item.status = SceneJobItemStatus.SKIPPED
        item.error_message = reason
        if started_monotonic is not None:
            item.elapsed_seconds = int(round(time.monotonic() - started_monotonic))
        item.finished_at = utcnow()
        db.commit()

    def _finalize(self, db: Session, job_id: int) -> None:
        """汇总任务终态：取消保持 cancelled，否则按条目结果定 success/partial/failed。"""
        # populate_existing：取消是在另一个会话里提交的，这里必须读最新状态，
        # 否则会把 cancelled 覆盖成 failed
        job = db.get(SceneJob, job_id, populate_existing=True)
        if job is None:
            return

        # 取消接口已经写过 finished_at，这里保持 cancelled 不覆盖
        if job.status == SceneJobStatus.CANCELLED:
            job.skipped_videos = sum(
                1 for item in job.items if item.status == SceneJobItemStatus.SKIPPED
            )
            job.child_pid = None
            self._clear_current_progress(job)
            db.commit()
            logger.info("镜头分割任务已取消 | id=%s", job_id)
            return

        failed = sum(1 for item in job.items if item.status == SceneJobItemStatus.FAILED)
        skipped = sum(1 for item in job.items if item.status == SceneJobItemStatus.SKIPPED)
        succeeded = job.total_videos - failed - skipped

        if failed == 0 and skipped == 0:
            job.status = SceneJobStatus.SUCCESS
        elif succeeded > 0:
            job.status = SceneJobStatus.PARTIAL
        else:
            job.status = SceneJobStatus.FAILED

        job.failed_videos = failed
        job.skipped_videos = skipped
        if failed:
            reasons = [
                f"{item.source_name}：{item.error_message.splitlines()[0] if item.error_message else '失败'}"
                for item in job.items
                if item.status == SceneJobItemStatus.FAILED
            ][:3]
            job.error_message = "；".join(reasons)
        job.child_pid = None
        self._clear_current_progress(job)
        job.finished_at = utcnow()
        db.commit()

        logger.info(
            "镜头分割任务结束 | id=%s | 状态=%s | 成功=%s | 失败=%s | 片段=%s",
            job.id, job.status, succeeded, failed, job.clip_count,
        )
