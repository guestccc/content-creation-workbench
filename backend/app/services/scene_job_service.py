"""智能镜头分割任务的业务逻辑层。

职责边界：
- 本模块只管「状态机 + 查询 + 创建时的文件枚举」，完全不碰子进程；
- 真正起 vct 子进程的执行逻辑在 scene_runner.py，工作线程在 scene_job_worker.py。

任务生命周期：

    pending ──工作线程认领──> running ──全部成功──> success
       │                       │
       │                       ├──部分失败──> partial
       │                       └──全部失败──> failed
       │
       └──取消──> cancelled（running 中取消：当前条目标记后同样落到 cancelled）

终态（success / partial / failed / cancelled）只能通过删除记录清理，
不提供自动重试 —— 视频文件损坏是确定性错误，自动重跑只会再浪费几分钟。
"""

import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import BadRequestError, ConflictError, DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.core.scene_templates import resolve_params
from app.db.session import transaction
from app.models.content import utcnow
from app.models.scene_job import (
    SceneJob,
    SceneJobItem,
    SceneJobItemStatus,
    SceneJobMode,
    SceneJobStatus,
)
from app.schemas.scene_job import SceneJobCreate

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# 视频枚举（模块级纯函数，便于单独测试）
# --------------------------------------------------------------------------


def is_video_file(name: str) -> bool:
    """按后端配置的扩展名白名单判断是否为可处理的视频文件。"""
    return Path(name).suffix.lower() in settings.SCENE_INPUT_EXTENSIONS


def enumerate_videos(
    input_path: str, *, recursive: bool, files: Optional[List[str]] = None
) -> List[Path]:
    """把输入路径展开成一份确定的视频清单。

    规则：
    - 输入是文件：直接用这一条（扩展名不在白名单也接受 —— 用户显式点名了它）；
    - 输入是目录：按扩展名白名单枚举，recursive 决定是否下钻子目录；
      跳过隐藏文件和 macOS 的 AppleDouble（._ 开头）文件；
    - 传了 files（前端勾选的文件名）时只保留勾选项。

    Returns:
        排序后的视频绝对路径列表。

    Raises:
        BadRequestError: 路径不存在、不是文件/目录、目录里没有可处理的视频、
            或勾选的文件名在目录中不存在。
    """
    source = Path(input_path)

    if not source.exists():
        raise BadRequestError(f"输入路径不存在：{input_path}")

    if source.is_file():
        return [source]

    if not source.is_dir():
        raise BadRequestError(f"输入路径既不是文件也不是目录：{input_path}")

    # 目录：枚举视频文件
    if files:
        # 前端勾选模式：只取勾选项，文件名已在 schema 层禁止路径分隔符
        candidates = [source / name for name in files]
        missing = [p.name for p in candidates if not p.is_file()]
        if missing:
            raise BadRequestError(f"以下文件在输入目录中不存在：{missing}")
        videos = candidates
    else:
        iterator = source.rglob("*") if recursive else source.iterdir()
        videos = [
            entry
            for entry in iterator
            if entry.is_file()
            and not entry.name.startswith(".")
            and is_video_file(entry.name)
        ]

    videos.sort(key=lambda p: str(p).lower())

    if not videos:
        raise BadRequestError(f"输入目录下没有可处理的视频文件：{input_path}")
    if len(videos) > settings.SCENE_MAX_BATCH_FILES:
        raise BadRequestError(
            f"一次最多处理 {settings.SCENE_MAX_BATCH_FILES} 条视频，当前共 {len(videos)} 条；"
            "请改用文件勾选或分批处理"
        )
    return videos


def _allocate_output_dirs(
    videos: List[Path], output_root: Path, *, check_existing_clips: bool
) -> Dict[Path, Path]:
    """为每条视频分配独立的输出子目录，并做预检与预建。

    目录名沿用 vct CLI 自己的默认约定 <视频名>_scenes；同一批里出现同名
    （recursive 模式下不同子目录里可能有同名文件）时追加 -2、-3 后缀。

    预建的原因：进度是按输出目录里 *_clip_*.mp4 的个数算的，目录保证是空的，
    计数才等于真实进度，不会被历史残留污染。所以 split 模式下如果目录已存在
    且已有片段，直接报错让用户换目录，而不是默默混着写。

    Args:
        videos: 视频清单。
        output_root: 输出根目录。
        check_existing_clips: split 模式传 True（已有片段则报错）；preview 模式
            输出在后端临时目录里，传 False。

    Raises:
        BadRequestError: 某个输出目录已存在切割结果，或输出目录无法创建。
    """
    allocation: Dict[Path, Path] = {}
    used_names: Dict[str, int] = {}

    for video in videos:
        base_name = f"{video.stem}_scenes"
        count = used_names.get(base_name, 0)
        used_names[base_name] = count + 1
        dir_name = base_name if count == 0 else f"{base_name}-{count + 1}"
        out_dir = output_root / dir_name

        if check_existing_clips and out_dir.is_dir() and list(out_dir.glob("*_clip_*.mp4")):
            raise BadRequestError(
                f"输出目录已存在同名切割结果：{out_dir}；请更换输出目录或先清理旧结果"
            )

        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BadRequestError(f"创建输出目录失败：{out_dir}（{exc}）") from exc

        allocation[video] = out_dir

    return allocation


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------


class SceneJobService:
    """镜头分割任务服务。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _get_job_or_404(self, job_id: int) -> SceneJob:
        """按 ID 取任务，不存在则抛 NotFoundError。"""
        job = self.db.get(SceneJob, job_id)
        if job is None:
            raise NotFoundError(f"镜头分割任务不存在：id={job_id}")
        return job

    def get_job(self, job_id: int) -> SceneJob:
        """按 ID 获取任务（含每个视频的明细）。"""
        try:
            return self._get_job_or_404(job_id)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("查询镜头分割任务失败 | id=%s", job_id)
            raise DatabaseError("查询镜头分割任务失败") from exc

    def list_jobs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Tuple[List[SceneJob], int]:
        """分页查询任务，按创建时间倒序。

        Returns:
            (当前页数据, 满足条件的总条数)
        """
        try:
            conditions = []
            if status:
                conditions.append(SceneJob.status == status)
            if mode:
                conditions.append(SceneJob.mode == mode)

            count_stmt = select(func.count()).select_from(SceneJob)
            list_stmt = select(SceneJob)
            for condition in conditions:
                count_stmt = count_stmt.where(condition)
                list_stmt = list_stmt.where(condition)

            total = self.db.execute(count_stmt).scalar_one()

            list_stmt = (
                list_stmt.order_by(SceneJob.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            items = list(self.db.execute(list_stmt).scalars().all())
            return items, int(total)
        except SQLAlchemyError as exc:
            logger.exception("查询镜头分割任务列表失败")
            raise DatabaseError("查询镜头分割任务列表失败") from exc

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def create_job(self, payload: SceneJobCreate) -> SceneJob:
        """创建镜头分割任务：枚举视频、预建输出目录、落库排队。

        创建成功即返回，真正的检测与切割由后台工作线程认领执行。

        Raises:
            BadRequestError: 输入路径/输出目录不合法，或目录里没有视频。
            DatabaseError: 写库失败（事务已回滚，已预建的空目录无害残留）。
        """
        videos = enumerate_videos(
            payload.input_path, recursive=payload.recursive, files=payload.files
        )
        params = resolve_params(
            payload.template,
            detector=payload.detector,
            threshold=payload.threshold,
            min_len=payload.min_len,
            copy_mode=payload.copy_mode,
        )

        # 预览模式不污染用户目录：CSV 写到后端管理的临时目录
        if payload.mode == SceneJobMode.PREVIEW:
            output_root = Path(tempfile.gettempdir()) / "vct-preview"
        else:
            # split 模式 output_dir 已在 schema 层保证非空
            output_root = Path(payload.output_dir or "")

        try:
            with transaction(self.db):
                job = SceneJob(
                    mode=payload.mode,
                    status=SceneJobStatus.PENDING,
                    input_path=payload.input_path,
                    output_dir=str(output_root),
                    recursive=payload.recursive,
                    params=params,
                    total_videos=len(videos),
                )
                self.db.add(job)
                self.db.flush()  # 先拿到 job.id，预览目录名里要用

                if payload.mode == SceneJobMode.PREVIEW:
                    # 每个任务一个独立的预览根目录，避免不同任务互相覆盖 CSV
                    output_root = output_root / f"job-{job.id}"
                    job.output_dir = str(output_root)

                allocation = _allocate_output_dirs(
                    videos,
                    output_root,
                    check_existing_clips=payload.mode == SceneJobMode.SPLIT,
                )

                for index, video in enumerate(videos, start=1):
                    self.db.add(
                        SceneJobItem(
                            job_id=job.id,
                            index=index,
                            source_path=str(video),
                            source_name=video.name,
                            output_dir=str(allocation[video]),
                            status=SceneJobItemStatus.PENDING,
                        )
                    )
                self.db.flush()

            logger.info(
                "镜头分割任务创建成功 | id=%s | mode=%s | 视频数=%s",
                job.id,
                job.mode,
                len(videos),
            )
            return job
        except (BadRequestError, NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("镜头分割任务创建失败 | input=%s", payload.input_path)
            raise DatabaseError("镜头分割任务创建失败") from exc

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------

    def cancel_job(self, job_id: int) -> SceneJob:
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

                if job.status in SceneJobStatus.TERMINAL:
                    raise ConflictError(f"任务已是终态（{job.status}），无法取消")

                job.status = SceneJobStatus.CANCELLED
                job.finished_at = utcnow()
                self.db.flush()

            logger.info("镜头分割任务已取消 | id=%s", job_id)
            return job
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("取消镜头分割任务失败 | id=%s", job_id)
            raise DatabaseError("取消镜头分割任务失败") from exc

    def delete_job(self, job_id: int) -> None:
        """删除任务记录（级联删除所有条目）。

        仅允许删除终态任务；切割产出的文件留在磁盘上不动 —— 那是用户的素材，
        删记录不应该顺手删文件。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务尚未结束。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                job = self._get_job_or_404(job_id)

                if job.status not in SceneJobStatus.TERMINAL:
                    raise ConflictError(
                        f"任务尚未结束（{job.status}），请先取消后再删除"
                    )

                self.db.delete(job)

            logger.info("镜头分割任务已删除 | id=%s", job_id)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("删除镜头分割任务失败 | id=%s", job_id)
            raise DatabaseError("删除镜头分割任务失败") from exc

    # ------------------------------------------------------------------
    # 结果汇总
    # ------------------------------------------------------------------

    def get_scenes_summary(self, job_id: int) -> Dict:
        """汇总预览模式下所有视频的切点，供「预览切点」结果区展示。

        Raises:
            NotFoundError: 任务不存在。
        """
        job = self.get_job(job_id)

        durations: List[float] = []
        for item in job.items:
            for scene in item.scenes or []:
                duration = scene.get("duration")
                if isinstance(duration, (int, float)):
                    durations.append(float(duration))

        return {
            "job_id": job.id,
            "status": job.status,
            "total_scenes": sum(item.scene_count for item in job.items),
            "total_duration": round(sum(durations), 3),
            "shortest": round(min(durations), 3) if durations else None,
            "longest": round(max(durations), 3) if durations else None,
            "average": (
                round(sum(durations) / len(durations), 3) if durations else None
            ),
            "items": job.items,
        }

    def list_clips(self, job_id: int) -> List[Dict]:
        """列出任务切出的所有片段（跨视频按条目顺序、再按文件名排序）。

        返回的 index 是全任务范围内从 1 开始的连续序号 —— 缩略图接口
        就用这个序号定位文件，避免前端传路径造成任意文件读取。

        每一条还带上 item_index（它属于哪一条视频）—— 前端要按视频分组的
        时候只能靠这个：递归模式下两条同名视频是很正常的，拿文件名去归组
        会把它们混在一起。

        Raises:
            NotFoundError: 任务不存在。
        """
        job = self.get_job(job_id)

        clips: List[Dict] = []
        for item in job.items:
            for name in sorted(item.clip_names or []):
                file_path = Path(item.output_dir) / name
                try:
                    size = file_path.stat().st_size
                except OSError:
                    # 文件可能被用户在磁盘上挪走了：仍然列出，大小记 0
                    size = 0
                clips.append(
                    {
                        "index": len(clips) + 1,
                        "item_index": item.index,
                        "name": name,
                        "source_name": item.source_name,
                        "size_bytes": size,
                        "path": file_path,
                    }
                )
        return clips

    def get_clip_path(self, job_id: int, clip_index: int) -> Tuple[Path, str]:
        """按全局序号定位一个片段文件（供缩略图接口使用）。

        路径完全由任务记录推导，不接受任何外部传入的路径片段。

        Raises:
            NotFoundError: 任务不存在或序号越界或文件已被移走。
        """
        clips = self.list_clips(job_id)
        if clip_index < 1 or clip_index > len(clips):
            raise NotFoundError(
                f"片段不存在：job={job_id}, index={clip_index}（共 {len(clips)} 个片段）"
            )
        clip = clips[clip_index - 1]
        path: Path = clip["path"]
        if not path.is_file():
            raise NotFoundError(f"片段文件已被移动或删除：{clip['name']}")
        return path, clip["name"]
