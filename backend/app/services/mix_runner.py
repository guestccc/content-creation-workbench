"""智能混剪的执行层：编排顺序、起 ffmpeg 子进程、盯进度、收尾。

与 scene_runner.py 的职责划分一致：service 管状态机与查询，本模块管
「真正把成片拼出来」。不依赖 FastAPI，所有外部依赖（会话工厂、Popen、
时钟参数）都可注入，测试传 FakePopen 在主线程同步跑完整个流程。

合成采用**两遍编码**（为什么不是单次 filter_complex：N 条成片只有中间段
顺序不同，单次方案会把全部片段重编码 N 遍；两遍方案每个片段只编码一次，
N 条成片各自只是流复制，N=5、20 个片段时省 70–80% 的耗时）：

- Pass 1 归一化：对本任务用到的片段（去重后的并集）逐个编码成统一规格的
  中间文件（720×1280 / 30fps / aac 48kHz 这类），落 `.tmp/mix_<job_id>/norm/`；
- Pass 2 拼接：每条成片一次 concat demuxer + `-c:v copy`（流复制，不重编码），
  音频重编码（AAC priming 样本让 copy 拼接产生间隙/爆音，不值得省这几秒）。

子进程管理沿用 scene_runner 的三条硬规则（stdio 落文件绝不 PIPE、
start_new_session=True 整组 kill、所有等待有界），实现都在 media_tools。
"""

import json
import math
import os
import random
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger
from app.core.materials import CLIPS, OUTPUT, materials_root, subdir
from app.db.session import SessionLocal, transaction
from app.models.content import utcnow
from app.models.mix_job import MixJob, MixJobItem, MixJobStatus, MixOutputStatus, MixPhase
from app.services.media_tools import (
    CHILD_CREATION_FLAGS,
    child_env,
    command_line,
    find_tool,
    probe_duration,
    read_log_tail,
    terminate_process_group,
)

logger = get_logger(__name__)

# ffmpeg 的 -progress 输出里我们关心的一行：out_time_us=12345678
_PROGRESS_TIME = re.compile(r"^out_time_us=(\d+)\s*$", re.MULTILINE)


# --------------------------------------------------------------------------
# 编排：纯函数，最好测
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MixPlan:
    """一条成片的完整编排。"""

    opening: List[str]  # 开头：用户顺序，原样保留
    middle: List[str]  # 中间：本条打乱后的顺序
    ending: List[str]  # 结尾：用户顺序，原样保留
    seed: int  # 本条使用的子种子（= 任务种子派生），随成片落库可复现
    order: List[str] = field(default_factory=list)  # 三段拼起来的最终顺序


def middle_permutation_limit(middle_count: int) -> int:
    """中间段能排出的不同顺序数（K!），同时作为 count 的上限。"""
    return math.factorial(middle_count)


def plan_outputs(
    opening: List[str], middle: List[str], ending: List[str], count: int, seed: int
) -> List[MixPlan]:
    """为 count 条成片各自编一份顺序：开头/结尾原样，中间段各自打乱且互不相同。

    第 k 条用 random.Random(seed * 1_000_003 + k) 洗牌 —— 种子随成片落库，
    任何时候都能复现。撞了已出现过的顺序就换下一个子种子重试（最多 50 次）。

    Raises:
        ValueError: count 超过中间段的排列数（K!）—— 超过必然重复，
            宁可在接口层拒绝，也不产出「看着随机其实重样」的成片。
    """
    limit = middle_permutation_limit(len(middle))
    if count > limit:
        raise ValueError(
            f"中间选了 {len(middle)} 条，最多只能排出 {limit} 种不同顺序"
        )

    plans: List[MixPlan] = []
    seen = set()
    k = 0
    retries = 0
    while len(plans) < count:
        k += 1
        sub_seed = seed * 1_000_003 + k
        shuffled = list(middle)
        random.Random(sub_seed).shuffle(shuffled)
        key = tuple(shuffled)
        if key in seen:
            retries += 1
            if retries > 50:
                # 排列数够但运气极差连撞 50 次：不猜着硬凑，直接报清况
                raise ValueError("中间段顺序去重失败，请重试")
            continue
        seen.add(key)
        order = list(opening) + shuffled + list(ending)
        plans.append(
            MixPlan(
                opening=list(opening),
                middle=shuffled,
                ending=list(ending),
                seed=sub_seed,
                order=order,
            )
        )
    return plans


# --------------------------------------------------------------------------
# ffprobe 规格探测（纯函数）
# --------------------------------------------------------------------------


def probe_video_spec(video: Path) -> Optional[dict]:
    """探测一段视频的完整规格：宽高、帧率、旋转、有无音轨、时长。

    与 media_tools.probe_duration 的区别：混剪要拿第一条素材的规格当
    成片规格，只有时长远远不够。失败返回 None（调用方据此失败，不猜）。

    旋转处理：带 rotate/display matrix 的竖屏素材，ffprobe 报的 width/height
    是**编码尺寸**不是显示尺寸，±90/±270 度时必须交换宽高，否则成片画幅算错。
    """
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_streams", "-show_format",
                "-of", "json",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=child_env(),
        )
        data = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None

    streams = data.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video_stream is None:
        return None
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)

    # 帧率：r_frame_rate 形如 "30/1"
    fps_num, fps_den = 30, 1
    raw_fps = str(video_stream.get("r_frame_rate") or "")
    if "/" in raw_fps:
        try:
            num, den = raw_fps.split("/", 1)
            if int(den) > 0:
                fps_num, fps_den = int(num), int(den)
        except ValueError:
            pass

    # 旋转：side_data_list 里的 rotation 或 displaymatrix
    rotation = 0
    for side in video_stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                rotation = int(side["rotation"])
            except (TypeError, ValueError):
                rotation = 0
    if abs(rotation) in (90, 270):
        width, height = height, width

    duration = None
    try:
        duration = round(float((data.get("format") or {}).get("duration")), 3)
    except (TypeError, ValueError):
        duration = None

    if width <= 0 or height <= 0:
        return None
    return {
        "width": width,
        "height": height,
        "fps_num": fps_num,
        "fps_den": fps_den,
        "rotation": rotation,
        "has_audio": has_audio,
        "duration": duration,
    }


# --------------------------------------------------------------------------
# ffmpeg 命令构造（纯函数，测试断言这些字符串）
# --------------------------------------------------------------------------


def build_normalize_filter(spec: dict, duration: float, has_audio: bool) -> str:
    """Pass 1 归一化的 filter_complex：视频链 + 音频链，分号分隔。

    视频链：等比缩放到目标画幅内 → 黑边补齐 → SAR=1 → 恒定帧率 → yuv420p →
    时间戳归零。force_divisible_by=2 是必需的：H.264/yuv420p 要求宽高为偶数。

    音频链统一到 48000Hz/立体声；先 atrim 截长再 apad 补短，把音频长度钉死在
    视频时长上，Pass 2 拼接点才不会音画漂移。aresample 必须在 aformat 之前
    （aformat 不做采样率转换）。无音轨时用 anullsrc 补静音 —— anullsrc 是
    无限长源，必须紧跟 atrim，否则编码永不结束。
    """
    width, height = spec["width"], spec["height"]
    fps = f"{spec['fps_num']}/{spec['fps_den']}"
    video_chain = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease"
        f":force_divisible_by=2,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={fps},format=yuv420p,setpts=PTS-STARTPTS[v]"
    )
    if has_audio:
        audio_chain = (
            f"[0:a]aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"atrim=start=0:end={duration},"
            f"apad=whole_dur={duration},"
            f"asetpts=PTS-STARTPTS[a]"
        )
    else:
        audio_chain = (
            f"anullsrc=channel_layout=stereo:sample_rate=48000,"
            f"atrim=start=0:end={duration},"
            f"asetpts=PTS-STARTPTS[a]"
        )
    return f"{video_chain};{audio_chain}"


def build_normalize_argv(
    ffmpeg: str, src: Path, dst: Path, spec: dict, duration: float,
    has_audio: bool, progress_path: Path,
) -> List[str]:
    """Pass 1 单片段归一化 argv。"""
    return [
        ffmpeg, "-hide_banner", "-nostats", "-y",
        "-i", str(src),
        "-filter_complex", build_normalize_filter(spec, duration, has_audio),
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264",
        "-preset", settings.MIX_ENCODE_PRESET,
        "-crf", str(settings.MIX_ENCODE_CRF),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-video_track_timescale", "15360",
        "-movflags", "+faststart",
        "-progress", str(progress_path),
        str(dst),
    ]


def write_concat_list(list_path: Path, norm_paths: List[Path]) -> None:
    """写 concat demuxer 清单：file '<绝对路径>'，单引号按 ffmpeg 规则转义。

    素材目录名含中文不是问题（文件内容 UTF-8 直传，不经 shell），
    但调用方必须带 -safe 0，否则 ffmpeg 拒绝绝对路径。
    """
    lines = []
    for path in norm_paths:
        escaped = str(path).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_concat_argv(
    ffmpeg: str, list_path: Path, dst: Path, progress_path: Path
) -> List[str]:
    """Pass 2 单条成片拼接 argv：视频流复制、音频重编码。"""
    return [
        ffmpeg, "-hide_banner", "-nostats", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-progress", str(progress_path),
        str(dst),
    ]


def read_ffmpeg_progress_us(progress_path: Path) -> Optional[int]:
    """从 ffmpeg -progress 文件里读最新的 out_time_us（微秒）。"""
    try:
        text = progress_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = _PROGRESS_TIME.findall(text)
    return int(matches[-1]) if matches else None


def probe_environment() -> dict:
    """探测混剪功能的运行环境，结果直接给「依赖自检」接口用。

    混剪只需要 ffmpeg 与 ffprobe（不需要 vct / scenedetect），
    但要把素材目录与默认输出目录一起带回，供页面初始化。
    """
    ffmpeg = find_tool("ffmpeg")
    ffprobe = find_tool("ffprobe")
    dependencies = [
        {
            "name": "ffmpeg",
            "ok": ffmpeg is not None,
            "path": ffmpeg or "",
            "detail": "片段归一化与成片拼接" if ffmpeg else "未在 PATH 中找到",
            "fix_hint": "" if ffmpeg else "brew install ffmpeg 或放入 ~/.local/bin",
        },
        {
            "name": "ffprobe",
            "ok": ffprobe is not None,
            "path": ffprobe or "",
            "detail": "素材规格与时长探测" if ffprobe else "未找到（无法确定成片规格）",
            "fix_hint": "" if ffprobe else "随 ffmpeg 一起安装",
        },
    ]
    clips_dir = subdir(CLIPS)
    return {
        "ready": ffmpeg is not None and ffprobe is not None,
        "materials_dir": str(materials_root()),
        # 「添加素材目录」选择器的默认起点：镜头切片目录还在就落在那儿，
        # 否则退到素材根，别给一个不存在的路径
        "default_source_dir": str(clips_dir if clips_dir.is_dir() else materials_root()),
        "default_output_dir": str(subdir(OUTPUT)),
        "dependencies": dependencies,
    }


def resolve_clip_path(raw: str) -> Path:
    """把任务里记的素材路径还原成磁盘路径。

    新任务存的是**绝对路径**（素材可以来自任意目录，没有共同的根）；
    改版前的历史任务存的是相对 materials 根的路径，这里一并兜住，
    免得老记录一执行就变成「素材不存在」。
    """
    path = Path(raw)
    return path if path.is_absolute() else materials_root() / path


# --------------------------------------------------------------------------
# 队列认领与孤儿回收（DB 即队列）
# --------------------------------------------------------------------------


def claim_next_pending_id(session_factory: sessionmaker = SessionLocal) -> Optional[int]:
    """取下一个待执行混剪任务的 ID（只读不改状态，真正抢锁在 run_job 里）。"""
    with session_factory() as db:
        row = db.execute(
            select(MixJob.id)
            .where(MixJob.status == MixJobStatus.PENDING)
            .order_by(MixJob.id.asc())
            .limit(1)
        ).first()
        return int(row[0]) if row else None


def is_our_child(pid: int) -> bool:
    """校验一个 PID 现在确实还是我们起的 ffmpeg 进程（防 PID 复用误杀）。"""
    if pid <= 0:
        return False
    command = command_line(pid)
    return "ffmpeg" in command


def recover_interrupted_jobs(
    session_factory: sessionmaker = SessionLocal,
    *,
    stop_grace_seconds: Optional[float] = None,
) -> int:
    """启动时回收上次异常退出留下的 running 混剪任务（与镜头分割同一套做法）。"""
    grace = (
        stop_grace_seconds
        if stop_grace_seconds is not None
        else settings.MIX_JOB_STOP_GRACE_SECONDS
    )
    recovered = 0

    with session_factory() as db:
        try:
            with transaction(db):
                jobs = list(
                    db.execute(
                        select(MixJob).where(MixJob.status == MixJobStatus.RUNNING)
                    )
                    .scalars()
                    .all()
                )
                for job in jobs:
                    if job.child_pid and is_our_child(job.child_pid):
                        logger.warning(
                            "回收残留的 ffmpeg 进程组 | job=%s | pid=%s", job.id, job.child_pid
                        )
                        terminate_process_group(job.child_pid, grace)
                    job.status = MixJobStatus.FAILED
                    job.error_message = "服务重启，任务中断"
                    job.finished_at = utcnow()
                    job.child_pid = None
                    for item in job.outputs:
                        if item.status == MixOutputStatus.RUNNING:
                            item.status = MixOutputStatus.FAILED
                            item.error_message = "服务重启，任务中断"
                            item.finished_at = utcnow()
                        elif item.status == MixOutputStatus.PENDING:
                            item.status = MixOutputStatus.SKIPPED
                    recovered += 1
                db.flush()
        except Exception:  # noqa: BLE001 - 启动恢复绝不能阻断服务启动
            logger.exception("回收中断的混剪任务失败")
            return 0

    if recovered:
        logger.info("已回收中断的混剪任务 | 数量=%s", recovered)
    return recovered


# --------------------------------------------------------------------------
# 执行器
# --------------------------------------------------------------------------


class MixRunner:
    """混剪执行器：两遍编码把 N 条成片拼出来并把进度落库。

    所有外部依赖都可注入，测试传 FakePopen + tick_seconds=0 即可在主线程
    同步跑完，不需要线程也不需要真视频。
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker = SessionLocal,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        tick_seconds: Optional[float] = None,
        progress_seconds: Optional[float] = None,
        job_timeout_seconds: Optional[int] = None,
        item_timeout_seconds: Optional[int] = None,
        stop_grace_seconds: Optional[float] = None,
    ) -> None:
        self.session_factory = session_factory
        self.popen_factory = popen_factory
        self.tick_seconds = (
            tick_seconds if tick_seconds is not None else settings.MIX_JOB_TICK_SECONDS
        )
        self.progress_seconds = (
            progress_seconds
            if progress_seconds is not None
            else settings.MIX_JOB_PROGRESS_SECONDS
        )
        self.job_timeout_seconds = (
            job_timeout_seconds
            if job_timeout_seconds is not None
            else settings.MIX_JOB_TIMEOUT_SECONDS
        )
        self.item_timeout_seconds = (
            item_timeout_seconds
            if item_timeout_seconds is not None
            else settings.MIX_JOB_ITEM_TIMEOUT_SECONDS
        )
        self.stop_grace_seconds = (
            stop_grace_seconds
            if stop_grace_seconds is not None
            else settings.MIX_JOB_STOP_GRACE_SECONDS
        )

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run_job(self, job_id: int, stop_event=None) -> bool:
        """认领并执行一个混剪任务。Returns: True 表示执行了该任务。"""
        with self.session_factory() as db:
            if not self._claim(db, job_id):
                return False

        with self.session_factory() as db:
            job = db.get(MixJob, job_id)
            assert job is not None  # _claim 成功说明存在
            logger.info(
                "开始执行混剪任务 | id=%s | 成片数=%s | 片段=%s+%s+%s",
                job.id, job.count, len(job.opening), len(job.middle), len(job.ending),
            )

            job_started = time.monotonic()
            tmp_dir = self._tmp_dir(job.id)
            norm_dir = tmp_dir / "norm"
            list_dir = tmp_dir / "lists"
            try:
                self._run(db, job, norm_dir, list_dir, job_started, stop_event)
            finally:
                self._cleanup_tmp(db, job, tmp_dir)

            self._finalize(db, job_id)
        return True

    def _run(self, db, job, norm_dir, list_dir, job_started, stop_event) -> None:
        """两遍编码主体：Pass 1 归一化去重并集，Pass 2 逐条流复制拼接。"""
        norm_dir.mkdir(parents=True, exist_ok=True)
        list_dir.mkdir(parents=True, exist_ok=True)

        # ---- 准备：还原素材路径，探测第一条素材的规格 ----
        unique_clips = list(dict.fromkeys(job.opening + job.middle + job.ending))
        abs_paths = {clip: resolve_clip_path(clip) for clip in unique_clips}
        missing = [clip for clip, path in abs_paths.items() if not path.is_file()]
        if missing:
            self._fail_whole_job(
                db, job,
                f"素材文件不存在（可能已被移动或删除）：{missing[0]} 等 {len(missing)} 个",
            )
            return

        first_spec = probe_video_spec(abs_paths[job.opening[0]])
        if first_spec is None:
            self._fail_whole_job(
                db, job, f"无法读取开头第一条素材的规格：{job.opening[0]}"
            )
            return
        job.target = {
            "width": first_spec["width"],
            "height": first_spec["height"],
            "fps_num": first_spec["fps_num"],
            "fps_den": first_spec["fps_den"],
        }

        # 每个片段的时长与音轨情况：归一化滤镜与总时长都要用
        specs: Dict[str, dict] = {}
        for rel in unique_clips:
            spec = probe_video_spec(abs_paths[rel])
            if spec is None:
                self._fail_whole_job(db, job, f"素材损坏或无法解析：{rel}")
                return
            specs[rel] = spec

        job.total_clips = len(unique_clips)
        job.done_clips = 0
        job.current_phase = MixPhase.NORMALIZE
        db.commit()

        # ---- Pass 1：逐片段归一化 ----
        failed_clips: Dict[str, str] = {}  # rel_path -> 失败原因
        for i, rel in enumerate(unique_clips, start=1):
            if self._should_stop(db, job, job_started):
                break
            norm_path = norm_dir / f"{i:04d}.mp4"
            ok, reason = self._normalize_clip(
                db, job, rel, abs_paths[rel], norm_path, specs[rel],
                done=i, total=len(unique_clips), job_started=job_started,
                stop_event=stop_event,
            )
            job.done_clips = i
            db.commit()
            if not ok:
                failed_clips[rel] = reason

        # ---- Pass 1 收尾：归一化失败的片段 → 牵连的成片直接标失败 ----
        if self._should_stop(db, job, job_started):
            self._skip_pending_outputs(db, job, reason="任务已取消或服务停止")
            return

        job.current_phase = MixPhase.CONCAT
        job.current_clip = ""
        db.commit()

        for item in job.outputs:
            if item.status != MixOutputStatus.PENDING:
                continue
            bad = [rel for rel in item.order if rel in failed_clips]
            if bad:
                self._finish_item(
                    db, job, item,
                    status=MixOutputStatus.FAILED,
                    started_monotonic=time.monotonic(),
                    exit_code=None,
                    error=f"片段归一化失败：{Path(bad[0]).name}（{failed_clips[bad[0]]}）",
                )
                continue

            if self._should_stop(db, job, job_started):
                self._skip_pending_outputs(db, job, reason="任务已取消或服务停止")
                return

            self._concat_output(db, job, item, norm_dir, list_dir, unique_clips, specs, job_started, stop_event)

    # ------------------------------------------------------------------
    # Pass 1：单片段归一化
    # ------------------------------------------------------------------

    def _normalize_clip(
        self, db, job, rel, src: Path, dst: Path, spec: dict,
        *, done: int, total: int, job_started: float, stop_event,
    ) -> tuple:
        """归一化一个片段。Returns: (成功与否, 失败原因)。"""
        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            return False, "未找到 ffmpeg"

        duration = spec.get("duration") or 0.0
        if duration <= 0:
            return False, "时长为 0 或无法探测"

        job.current_clip = Path(rel).name
        job.current_phase = MixPhase.NORMALIZE
        db.commit()

        progress_path = dst.parent / "progress.txt"
        # 日志放任务临时目录根而不是 norm/：失败收尾会删掉 norm/ 省磁盘，
        # 日志若放里面会跟着没了，用户就看不到 ffmpeg 的真实报错
        log_path = dst.parent.parent / f"ffmpeg_norm_{done:04d}.log"
        argv = build_normalize_argv(
            ffmpeg, src, dst, job.target, duration, spec["has_audio"], progress_path
        )

        started = time.monotonic()
        exit_code, aborted = self._run_ffmpeg(
            db, job, argv, log_path, started,
            timeout_seconds=self.item_timeout_seconds,
            job_started=job_started, stop_event=stop_event,
            progress_hint=duration,
        )
        if aborted:
            return False, {"cancelled": "任务已取消", "stopped": "服务停止", "timeout": "归一化超时"}[aborted]
        if exit_code != 0:
            tail = read_log_tail(log_path)
            return False, tail[-200:] if tail else f"ffmpeg 退出码 {exit_code}"
        if not dst.is_file():
            return False, "归一化未产出文件"
        return True, ""

    # ------------------------------------------------------------------
    # Pass 2：单条成片拼接
    # ------------------------------------------------------------------

    def _concat_output(
        self, db, job, item: MixJobItem, norm_dir, list_dir,
        unique_clips, specs, job_started, stop_event,
    ) -> None:
        """拼接一条成片：写 concat 清单 → ffmpeg 流复制 → 原子改名就位。"""
        started = time.monotonic()
        item.status = MixOutputStatus.RUNNING
        item.started_at = utcnow()
        job.current_index = item.index
        db.commit()

        # 片段 rel → 归一化产物的映射（与 Pass 1 的编号规则一致）
        norm_of = {rel: norm_dir / f"{i:04d}.mp4" for i, rel in enumerate(unique_clips, start=1)}
        norm_paths = [norm_of[rel] for rel in item.order]

        out_dir = Path(job.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        final_path = out_dir / f"{item.index:02d}.mp4"
        partial_path = out_dir / f"{item.index:02d}.partial.mp4"
        list_path = list_dir / f"concat_{item.index:02d}.txt"
        progress_path = norm_dir / "progress.txt"
        # 与归一化日志同理：放临时目录根，失败收尾删 norm/ 时日志还在
        log_path = norm_dir.parent / f"ffmpeg_concat_{item.index:02d}.log"

        try:
            write_concat_list(list_path, norm_paths)
            ffmpeg = find_tool("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("未找到 ffmpeg")
            argv = build_concat_argv(ffmpeg, list_path, partial_path, progress_path)

            total_duration = sum(specs[rel].get("duration") or 0 for rel in item.order)
            exit_code, aborted = self._run_ffmpeg(
                db, job, argv, log_path, started,
                timeout_seconds=self.item_timeout_seconds,
                job_started=job_started, stop_event=stop_event,
                progress_hint=total_duration,
            )

            if aborted == "cancelled":
                partial_path.unlink(missing_ok=True)
                self._finish_item(
                    db, job, item, status=MixOutputStatus.SKIPPED,
                    started_monotonic=started, exit_code=exit_code, error="任务已取消",
                )
                return
            if aborted == "stopped":
                partial_path.unlink(missing_ok=True)
                self._finish_item(
                    db, job, item, status=MixOutputStatus.FAILED,
                    started_monotonic=started, exit_code=exit_code, error="服务停止，任务中断",
                )
                return
            if aborted == "timeout":
                partial_path.unlink(missing_ok=True)
                self._finish_item(
                    db, job, item, status=MixOutputStatus.FAILED,
                    started_monotonic=started, exit_code=exit_code,
                    error=f"拼接超时（超过 {self.item_timeout_seconds} 秒），已终止",
                )
                return

            if exit_code != 0 or not partial_path.is_file():
                tail = read_log_tail(log_path)
                partial_path.unlink(missing_ok=True)
                self._finish_item(
                    db, job, item, status=MixOutputStatus.FAILED,
                    started_monotonic=started, exit_code=exit_code,
                    error=tail[-300:] if tail else f"ffmpeg 退出码 {exit_code}，未产出成片",
                )
                return

            # 半成品永远不会以成片的文件名出现：先 .partial 再原子改名
            os.replace(partial_path, final_path)
            item.output_path = str(final_path)
            item.output_name = final_path.name
            item.duration_seconds = round(total_duration, 3) if total_duration else probe_duration(final_path)
            item.size_bytes = final_path.stat().st_size
            self._finish_item(
                db, job, item, status=MixOutputStatus.SUCCESS,
                started_monotonic=started, exit_code=exit_code, error="",
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不能中断整个批次
            logger.exception("成片拼接出现异常 | job=%s | item=%s", job.id, item.index)
            partial_path.unlink(missing_ok=True)
            self._finish_item(
                db, job, item, status=MixOutputStatus.FAILED,
                started_monotonic=started, exit_code=None, error=f"拼接异常：{exc}",
            )

    # ------------------------------------------------------------------
    # 子进程执行与等待（两个 pass 共用）
    # ------------------------------------------------------------------

    def _run_ffmpeg(
        self, db, job, argv, log_path: Path, started: float,
        *, timeout_seconds: int, job_started: float, stop_event,
        progress_hint: float = 0.0,
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
            )
            self._set_child_pid(db, job.id, process.pid)

            last_write = 0.0
            while True:
                exit_code = process.poll()
                if exit_code is not None:
                    return exit_code, None

                if self._is_cancelled(db, job.id):
                    logger.info("任务被取消，终止 ffmpeg | job=%s | pid=%s", job.id, process.pid)
                    terminate_process_group(process.pid, self.stop_grace_seconds)
                    return process.poll(), "cancelled"
                if stop_event is not None and stop_event.is_set():
                    terminate_process_group(process.pid, self.stop_grace_seconds)
                    return process.poll(), "stopped"
                if time.monotonic() - started > timeout_seconds:
                    terminate_process_group(process.pid, self.stop_grace_seconds)
                    return process.poll(), "timeout"
                if time.monotonic() - job_started > self.job_timeout_seconds:
                    terminate_process_group(process.pid, self.stop_grace_seconds)
                    return process.poll(), "timeout"

                now = time.monotonic()
                if now - last_write >= self.progress_seconds and progress_hint > 0:
                    last_write = now
                    self._refresh_progress(db, job, progress_hint)

                time.sleep(self.tick_seconds)
        finally:
            self._set_child_pid(db, job.id, None)
            if log_handle is not None:
                try:
                    log_handle.close()
                except OSError:
                    pass

    def _refresh_progress(self, db, job, progress_hint: float) -> None:
        """把 ffmpeg 的 out_time_us 换算成当前阶段内的百分比落库。

        分母是真实的（该片段/该成片的总时长，建计划时已 ffprobe 过），
        不编造权重。读不到进度文件就保持上一次的值，别让数字跳。
        """
        progress_path = self._tmp_dir(job.id) / "norm" / "progress.txt"
        out_us = read_ffmpeg_progress_us(progress_path)
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
                    update(MixJob)
                    .where(MixJob.id == job_id, MixJob.status == MixJobStatus.PENDING)
                    .values(status=MixJobStatus.RUNNING, started_at=utcnow())
                )
                return result.rowcount == 1
        except Exception:  # noqa: BLE001 - 抢锁失败只说明这条不归我们
            logger.exception("认领混剪任务失败 | id=%s", job_id)
            return False

    def _is_cancelled(self, db: Session, job_id: int) -> bool:
        """从库里读最新状态（列查询每次都真实执行 SQL，不受会话缓存影响）。"""
        status = db.execute(
            select(MixJob.status).where(MixJob.id == job_id)
        ).scalar_one_or_none()
        return status == MixJobStatus.CANCELLED

    def _should_stop(self, db, job, job_started: float) -> bool:
        """归一化/拼接循环每次迭代前的统一刹车点：取消或任务级超时。"""
        return (
            self._is_cancelled(db, job.id)
            or time.monotonic() - job_started > self.job_timeout_seconds
        )

    def _set_child_pid(self, db: Session, job_id: int, pid: Optional[int]) -> None:
        """更新当前子进程句柄（取消、超时、孤儿回收共用的唯一依据）。"""
        db.execute(update(MixJob).where(MixJob.id == job_id).values(child_pid=pid))
        db.commit()

    def _finish_item(self, db, job, item, *, status, started_monotonic, exit_code, error) -> None:
        """收尾一条成片：写结果、累计任务级计数。"""
        item.status = status
        item.exit_code = exit_code
        item.error_message = error[:4000] if error else ""
        item.elapsed_seconds = int(round(time.monotonic() - started_monotonic))
        item.finished_at = utcnow()

        job.completed_outputs += 1
        if status == MixOutputStatus.FAILED:
            job.failed_outputs += 1
        elif status == MixOutputStatus.SKIPPED:
            job.skipped_outputs += 1
        job.child_pid = None
        db.commit()

        logger.info(
            "成片拼接完成 | job=%s | item=%s | 状态=%s | 耗时=%ss",
            job.id, item.index, status, item.elapsed_seconds,
        )

    def _skip_pending_outputs(self, db, job, *, reason: str) -> None:
        """把剩余未执行的成片全部标记 skipped（取消/停止时的批量收尾）。"""
        for item in job.outputs:
            if item.status == MixOutputStatus.PENDING:
                item.status = MixOutputStatus.SKIPPED
                item.error_message = reason
                item.finished_at = utcnow()
                job.completed_outputs += 1
                job.skipped_outputs += 1
        db.commit()

    def _fail_whole_job(self, db, job, reason: str) -> None:
        """准备阶段就失败（素材丢失/损坏）：所有成片标失败，任务由 _finalize 汇总。"""
        for item in job.outputs:
            if item.status == MixOutputStatus.PENDING:
                self._finish_item(
                    db, job, item,
                    status=MixOutputStatus.FAILED,
                    started_monotonic=time.monotonic(),
                    exit_code=None, error=reason,
                )

    def _tmp_dir(self, job_id: int) -> Path:
        """任务临时目录：materials/output/.tmp/mix_<job_id>/。"""
        return subdir(OUTPUT) / ".tmp" / f"mix_{job_id}"

    def _cleanup_tmp(self, db, job, tmp_dir: Path) -> None:
        """清理临时目录。

        成功 → 全删；失败 → 保留 ffmpeg 日志、删掉 norm/ 省磁盘（日志尾部已入 DB）；
        取消 → 全删。
        """
        # populate_existing：取消是在另一个会话里提交的，必须读最新状态
        fresh = db.get(MixJob, job.id, populate_existing=True)
        status = fresh.status if fresh else job.status
        try:
            if status == MixJobStatus.CANCELLED:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            else:
                has_failure = any(
                    item.status == MixOutputStatus.FAILED for item in job.outputs
                )
                if has_failure:
                    shutil.rmtree(tmp_dir / "norm", ignore_errors=True)
                    (tmp_dir / "norm").mkdir(parents=True, exist_ok=True)
                else:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError as exc:
            # 临时文件清不掉只是占磁盘，不该把已经跑完的任务标失败
            logger.warning("清理混剪临时目录失败 | job=%s | %s", job.id, exc)

    @staticmethod
    def _clear_current_progress(job: MixJob) -> None:
        """任务结束时抹掉「当前正在做什么」那一组字段，保持接口数据自洽。"""
        job.current_clip = ""
        job.current_phase = ""
        job.progress_percent = 0.0

    def _finalize(self, db: Session, job_id: int) -> None:
        """汇总任务终态：取消保持 cancelled，否则按成片结果定 success/partial/failed。"""
        job = db.get(MixJob, job_id, populate_existing=True)
        if job is None:
            return

        if job.status == MixJobStatus.CANCELLED:
            job.skipped_outputs = sum(
                1 for item in job.outputs if item.status == MixOutputStatus.SKIPPED
            )
            job.child_pid = None
            self._clear_current_progress(job)
            db.commit()
            logger.info("混剪任务已取消 | id=%s", job_id)
            return

        failed = sum(1 for item in job.outputs if item.status == MixOutputStatus.FAILED)
        skipped = sum(1 for item in job.outputs if item.status == MixOutputStatus.SKIPPED)
        succeeded = job.total_outputs - failed - skipped

        if failed == 0 and skipped == 0:
            job.status = MixJobStatus.SUCCESS
        elif succeeded > 0:
            job.status = MixJobStatus.PARTIAL
        else:
            job.status = MixJobStatus.FAILED

        job.failed_outputs = failed
        job.skipped_outputs = skipped
        if failed:
            reasons = [
                f"第{item.index}条：{item.error_message.splitlines()[0] if item.error_message else '失败'}"
                for item in job.outputs
                if item.status == MixOutputStatus.FAILED
            ][:3]
            job.error_message = "；".join(reasons)
        job.child_pid = None
        self._clear_current_progress(job)
        job.finished_at = utcnow()
        db.commit()

        logger.info(
            "混剪任务结束 | id=%s | 状态=%s | 成功=%s | 失败=%s",
            job.id, job.status, succeeded, failed,
        )
