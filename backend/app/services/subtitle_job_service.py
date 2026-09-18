"""字幕提取任务的业务逻辑层。

职责边界（与 scene_job_service.py 一致）：
- 本模块只管「状态机 + 查询 + 创建时的文件枚举」，完全不碰子进程；
- 真正起 VideoCaptioner 子进程的执行逻辑在 subtitle_runner.py，
  工作线程在 subtitle_job_worker.py。

任务生命周期：

    pending ──工作线程认领──> running ──全部成功──> success
       │                       │
       │                       ├──部分失败──> partial
       │                       └──全部失败──> failed
       │
       └──取消──> cancelled（running 中取消：未开始的条目标记 skipped）

终态（success / partial / failed / cancelled）只能通过删除记录清理。
不提供自动重试：视频没声音、文件损坏都是确定性错误，自动重跑只会再烧一遍时间。
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core import video_files
from app.core.config import settings
from app.core.exceptions import BadRequestError, ConflictError, DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.core.materials import SUBTITLE, subdir
from app.core.subtitle_asr import resolve_engine
from app.db.session import transaction
from app.models.content import utcnow
from app.models.subtitle_job import (
    SubtitleJob,
    SubtitleJobItem,
    SubtitleJobItemStatus,
    SubtitleJobStatus,
)
from app.schemas.subtitle_job import OUTPUT_FORMAT, SubtitleJobCreate
from app.services.subtitle_env import detect

logger = get_logger(__name__)


def is_video_file(name: str) -> bool:
    """按后端配置的扩展名白名单判断是否为可处理的视频文件。"""
    return video_files.is_video_file(name, settings.SUBTITLE_INPUT_EXTENSIONS)


def enumerate_videos(
    input_path: str, *, recursive: bool, files: Optional[List[str]] = None
) -> List[Path]:
    """把输入路径展开成一份确定的视频清单（白名单与批量上限取本功能的配置）。

    规则与异常见 core/video_files.enumerate_videos —— 那里是唯一实现。
    """
    return video_files.enumerate_videos(
        input_path,
        recursive=recursive,
        files=files,
        extensions=settings.SUBTITLE_INPUT_EXTENSIONS,
        max_files=settings.SUBTITLE_MAX_BATCH_FILES,
    )


def allocate_output_paths(
    videos: List[Path], output_dir: Path, *, format_suffix: str = f".{OUTPUT_FORMAT}"
) -> Dict[Path, Path]:
    """为每条视频分配一个字幕输出文件，重名的加 -2 / -3 后缀。

    与镜头分割不同，这里**允许**目标目录里已经有同名文件：字幕是「一条视频
    一份」的产物，用户很可能分批提同一条视频（换识别语言再跑一次）。所以不报错，
    改成换个名字，谁都不覆盖谁。

    预建输出目录（而不是等到子进程里）：`-o` 传的是带扩展名的完整文件路径，
    VideoCaptioner 的文件模式分支不会自己创建父目录 —— 目录必须在这里就位。

    Raises:
        BadRequestError: 输出目录建不出来（权限、只读盘）。
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BadRequestError(f"创建输出目录失败：{output_dir}（{exc}）") from exc

    allocation: Dict[Path, Path] = {}
    used_names: Dict[str, int] = {}

    for video in videos:
        base_name = video.stem
        count = used_names.get(base_name, 0)

        # 同批次内重名（recursive 模式下不同子目录可能有同名文件）与磁盘上
        # 已有同名文件，走同一套「往后找第一个没被占的编号」逻辑：
        # 本批次已经分出去的编号会被记进 used_names，所以下一轮从它之后接着找。
        while True:
            count += 1
            candidate = output_dir / (
                f"{base_name}{format_suffix}"
                if count == 1
                else f"{base_name}-{count}{format_suffix}"
            )
            if not candidate.exists():
                break

        used_names[base_name] = count
        allocation[video] = candidate

    return allocation


class SubtitleJobService:
    """字幕提取任务服务。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _get_job_or_404(self, job_id: int) -> SubtitleJob:
        """按 ID 取任务，不存在则抛 NotFoundError。"""
        job = self.db.get(SubtitleJob, job_id)
        if job is None:
            raise NotFoundError(f"字幕提取任务不存在：id={job_id}")
        return job

    def get_job(self, job_id: int) -> SubtitleJob:
        """按 ID 获取任务（含每条视频的明细）。"""
        try:
            return self._get_job_or_404(job_id)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("查询字幕提取任务失败 | id=%s", job_id)
            raise DatabaseError("查询字幕提取任务失败") from exc

    def list_jobs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None,
    ) -> Tuple[List[SubtitleJob], int]:
        """分页查询任务，按创建时间倒序（即 ID 倒序）。

        Returns:
            (当前页数据, 满足条件的总条数)
        """
        try:
            conditions = []
            if status:
                conditions.append(SubtitleJob.status == status)

            count_stmt = select(func.count()).select_from(SubtitleJob)
            list_stmt = select(SubtitleJob)
            for condition in conditions:
                count_stmt = count_stmt.where(condition)
                list_stmt = list_stmt.where(condition)

            total = self.db.execute(count_stmt).scalar_one()

            list_stmt = (
                list_stmt.order_by(SubtitleJob.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            items = list(self.db.execute(list_stmt).scalars().all())
            return items, int(total)
        except SQLAlchemyError as exc:
            logger.exception("查询字幕提取任务列表失败")
            raise DatabaseError("查询字幕提取任务列表失败") from exc

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def create_job(self, payload: SubtitleJobCreate) -> SubtitleJob:
        """创建字幕提取任务：枚举视频、分配输出文件、落库排队。

        创建成功即返回，真正的转写由后台工作线程认领执行。

        Raises:
            BadRequestError: 未装 VideoCaptioner、输入路径不合法、目录里没有视频、
                输出目录建不出来。
            DatabaseError: 写库失败（事务已回滚）。
        """
        # 建任务前先确认工具可用：探测结果有缓存，这里只是读缓存不贵。
        # 失败要快 —— 让用户在点击时就看到原因，而不是排队几分钟后整条失败。
        if not detect().installed:
            raise BadRequestError(
                "未检测到可用的 VideoCaptioner，请先按页面上方的指引安装后再试"
            )

        videos = enumerate_videos(
            payload.input_path, recursive=payload.recursive, files=payload.files
        )
        output_dir = Path(payload.output_dir) if payload.output_dir else subdir(SUBTITLE)

        params = {
            "asr": resolve_engine(payload.asr),
            "language": payload.language or "",
            "format": OUTPUT_FORMAT,
        }

        try:
            with transaction(self.db):
                job = SubtitleJob(
                    status=SubtitleJobStatus.PENDING,
                    input_path=payload.input_path,
                    output_dir=str(output_dir),
                    recursive=payload.recursive,
                    params=params,
                    total_videos=len(videos),
                )
                self.db.add(job)
                self.db.flush()

                allocation = allocate_output_paths(videos, output_dir)

                for index, video in enumerate(videos, start=1):
                    self.db.add(
                        SubtitleJobItem(
                            job_id=job.id,
                            index=index,
                            source_path=str(video),
                            source_name=video.name,
                            output_path=str(allocation[video]),
                            status=SubtitleJobItemStatus.PENDING,
                        )
                    )
                self.db.flush()

            logger.info(
                "字幕提取任务创建成功 | id=%s | 视频数=%s | 引擎=%s | 输出=%s",
                job.id,
                len(videos),
                params["asr"],
                output_dir,
            )
            return job
        except (BadRequestError, NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("字幕提取任务创建失败 | input=%s", payload.input_path)
            raise DatabaseError("字幕提取任务创建失败") from exc

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------

    def cancel_job(self, job_id: int) -> SubtitleJob:
        """取消任务。

        pending 任务：直接置 cancelled，工作线程认领时会跳过；
        running 任务：置 cancelled 后由执行线程在下一次 tick（≤0.5 秒）发现，
        杀掉当前子进程组、把未执行的条目标记为 skipped。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务已处于终态。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)

                if job.status in SubtitleJobStatus.TERMINAL:
                    raise ConflictError(f"任务已是终态（{job.status}），无法取消")

                job.status = SubtitleJobStatus.CANCELLED
                job.finished_at = utcnow()
                self.db.flush()

            logger.info("字幕提取任务已取消 | id=%s", job_id)
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("取消字幕提取任务失败 | id=%s", job_id)
            raise DatabaseError("取消字幕提取任务失败") from exc

    def delete_job(self, job_id: int) -> None:
        """删除任务记录（级联删除所有条目）。

        仅允许删除终态任务；已经导出的 .srt 留在磁盘上不动 —— 那是用户的产物，
        删记录不应该顺手删文件。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务尚未结束。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)

                if job.status not in SubtitleJobStatus.TERMINAL:
                    raise ConflictError(f"任务尚未结束（{job.status}），请先取消后再删除")

                self.db.delete(job)

            logger.info("字幕提取任务已删除 | id=%s", job_id)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("删除字幕提取任务失败 | id=%s", job_id)
            raise DatabaseError("删除字幕提取任务失败") from exc

    # ------------------------------------------------------------------
    # 产物
    # ------------------------------------------------------------------

    def list_subtitles(self, job_id: int) -> List[Dict]:
        """列出任务产出的字幕文件（按条目顺序）。

        返回的 index 是全任务范围内从 1 开始的连续序号 —— 预览接口就用这个
        序号定位文件，避免前端传路径造成任意文件读取。

        与镜头分割的 list_clips 不同，这里**不检查文件是否存在**：条目在创建
        任务时就带上了确定的输出路径，列表要展示「应该产出哪些、哪些已经好了」，
        而不是只展示已存在的那些。

        Raises:
            NotFoundError: 任务不存在。
        """
        job = self.get_job(job_id)

        files: List[Dict] = []
        for item in job.items:
            path = Path(item.output_path)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            files.append(
                {
                    "index": len(files) + 1,
                    "item_index": item.index,
                    "name": path.name,
                    "source_name": item.source_name,
                    "size_bytes": size,
                    "segment_count": item.segment_count,
                    "exists": size > 0,
                    "path": path,
                }
            )
        return files

    def get_subtitle_path(self, job_id: int, index: int) -> Tuple[Path, str, str]:
        """按全局序号定位一个字幕文件（供预览接口使用）。

        路径完全由任务记录推导，不接受任何外部传入的路径片段 —— 放开让前端
        传路径就是任意文件读取漏洞。

        Returns:
            (文件路径, 文件名, 来源视频名)

        Raises:
            NotFoundError: 任务不存在、序号越界、或文件还没生成/已被移走。
        """
        files = self.list_subtitles(job_id)
        if index < 1 or index > len(files):
            raise NotFoundError(
                f"字幕文件不存在：job={job_id}, index={index}（共 {len(files)} 份）"
            )
        entry = files[index - 1]
        path: Path = entry["path"]
        if not path.is_file():
            raise NotFoundError(f"字幕文件还没有生成，或已被移动：{entry['name']}")
        return path, entry["name"], entry["source_name"]
