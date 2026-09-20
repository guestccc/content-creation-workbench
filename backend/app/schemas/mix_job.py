"""智能混剪相关的 Pydantic 模型。

三组：
1. 素材库 —— 素材目录清单、扫描结果与环境自检；
2. 任务创建 —— 三个片段列表 + 条数 + 输出目录；
3. 任务响应 —— 进度与每条成片的结果。

素材一律用 clip id（sha1(绝对路径)[:16]）而不是路径：URL 里不出现中文、
长度可控、杜绝路径穿越。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer, field_validator

from app.models.mix_job import MixJob, MixJobItem
from app.schemas.common import TimestampMixin, to_utc_iso


# --------------------------------------------------------------------------
# 素材库与环境自检
# --------------------------------------------------------------------------


class MixSourceItem(BaseModel):
    """用户添加的一个素材目录。"""

    id: str = Field(description="素材目录 id（sha1(绝对路径)[:16]）")
    path: str = Field(description="目录绝对路径")
    name: str = Field(description="目录名（展示用）")
    exists: bool = Field(description="目录当前是否还在（被删或盘没挂上时为 false）")
    added_at: float = Field(default=0.0, description="添加时间戳（秒）")
    clip_count: int = Field(default=0, description="收录到的视频数（扫描结果才有）")
    truncated: bool = Field(default=False, description="是否因超过单目录上限被截断")


class MixSourceCreate(BaseModel):
    """添加素材目录。"""

    path: str = Field(
        ..., min_length=1, max_length=2000, description="素材目录绝对路径（可以是任意本地目录）"
    )


class MixClipItem(BaseModel):
    """素材库中的一段视频。"""

    id: str = Field(description="片段 id（sha1(绝对路径)[:16]，接口参数用它而不是路径）")
    name: str = Field(description="文件名")
    group: str = Field(description="所属子目录（相对素材目录，空表示直接放在根下）")
    rel_path: str = Field(description="相对所属素材目录的 POSIX 路径")
    abs_path: str = Field(description="绝对路径（展示与排障用）")
    source_id: str = Field(description="所属素材目录 id")
    duration: Optional[float] = Field(default=None, description="时长（秒），探测失败为空")
    size_bytes: int = Field(description="文件大小（字节）")
    thumb_url: str = Field(description="缩略图地址（首次访问时后端才抽帧生成）")
    video_url: str = Field(description="视频流地址（支持 Range，可拖动进度条播放）")


class MixLibraryData(BaseModel):
    """素材库扫描结果。"""

    sources: List[MixSourceItem] = Field(description="已添加的素材目录清单")
    clips: List[MixClipItem] = Field(description="全部素材（跨目录去重后）")
    scanned_at: float = Field(description="扫描时间戳（秒）")


class MixDependencyStatus(BaseModel):
    """单个外部依赖的探测结果。"""

    name: str = Field(description="依赖名称")
    ok: bool = Field(description="是否可用")
    path: str = Field(default="", description="解析到的可执行文件路径")
    detail: str = Field(default="", description="探测说明")
    fix_hint: str = Field(default="", description="不可用时的修复命令，直接展示给用户")


class MixEnvironmentResponse(BaseModel):
    """混剪功能的运行环境自检结果。"""

    ready: bool = Field(description="ffmpeg 与 ffprobe 是否都可用")
    materials_dir: str = Field(description="素材目录根（内部产物落在这里）")
    default_source_dir: str = Field(description="添加素材目录时选择器的默认起始位置")
    default_output_dir: str = Field(description="默认输出目录（materials/output）")
    dependencies: List[MixDependencyStatus] = Field(description="各依赖的探测明细")


# --------------------------------------------------------------------------
# 任务创建
# --------------------------------------------------------------------------


class MixJobCreate(BaseModel):
    """创建混剪任务。

    三个列表都传 clip id 数组；开头与结尾的顺序就是拼接顺序，
    中间的顺序无意义（系统为每条成片各自打乱）。
    """

    opening: List[str] = Field(..., min_length=1, description="开头片段 id 列表，按选择顺序拼接")
    middle: List[str] = Field(..., min_length=1, description="中间片段 id 列表，系统随机打乱")
    ending: List[str] = Field(..., min_length=1, description="结尾片段 id 列表，按选择顺序拼接")
    count: int = Field(default=1, ge=1, le=20, description="要产出几条成片")
    output_dir: str = Field(..., max_length=1000, description="输出目录（绝对路径）")

    @field_validator("count")
    @classmethod
    def _check_count(cls, value: int) -> int:
        from app.core.config import settings
        if value > settings.MIX_MAX_OUTPUTS:
            raise ValueError(f"一次最多混剪 {settings.MIX_MAX_OUTPUTS} 条")
        return value


# --------------------------------------------------------------------------
# 任务响应
# --------------------------------------------------------------------------


class MixOutputItemResponse(BaseModel):
    """任务中的一条成片。"""

    id: int = Field(description="主键")
    index: int = Field(description="成片序号（从 1 开始）")
    order: List[str] = Field(description="该条成片的完整最终顺序（素材绝对路径）")
    status: str = Field(description="该条成片的状态")
    output_path: str = Field(description="成片绝对路径（成功后才填）")
    output_name: str = Field(description="成片文件名")
    duration_seconds: Optional[float] = Field(default=None, description="成片时长（秒）")
    size_bytes: int = Field(description="成片文件大小（字节）")
    exit_code: Optional[int] = Field(default=None, description="子进程退出码")
    elapsed_seconds: int = Field(description="该条成片耗时（秒）")
    error_message: str = Field(description="失败原因")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")
    thumb_url: str = Field(default="", description="封面地址（成功后才可访问）")
    video_url: str = Field(default="", description="播放地址（支持 Range，成功后才可访问）")

    @field_serializer("started_at", "finished_at")
    def _serialize_optional_times(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value) if value is not None else None

    @classmethod
    def from_model(cls, model: MixJobItem, job_id: int) -> "MixOutputItemResponse":
        """由 ORM 对象构造响应模型，顺手拼上媒体 URL。"""
        base = f"/api/v1/mix/jobs/{job_id}/outputs/{model.index}"
        has_output = model.status == "success" and bool(model.output_path)
        return cls(
            id=model.id,
            index=model.index,
            order=list(model.order or []),
            status=model.status,
            output_path=model.output_path,
            output_name=model.output_name,
            duration_seconds=model.duration_seconds,
            size_bytes=model.size_bytes,
            exit_code=model.exit_code,
            elapsed_seconds=model.elapsed_seconds,
            error_message=model.error_message,
            started_at=model.started_at,
            finished_at=model.finished_at,
            thumb_url=f"{base}/thumb" if has_output else "",
            video_url=f"{base}/video" if has_output else "",
        )


class MixJobResponse(TimestampMixin):
    """混剪任务详情。"""

    id: int = Field(description="主键")
    status: str = Field(description="任务状态")
    opening: List[str] = Field(description="开头素材清单（绝对路径，按用户顺序）")
    middle: List[str] = Field(description="中间素材清单（绝对路径，顺序无意义）")
    ending: List[str] = Field(description="结尾素材清单（绝对路径，按用户顺序）")
    count: int = Field(description="要产出几条成片")
    output_dir: str = Field(description="成片输出目录")
    seed: int = Field(description="随机种子（同种子可复现完全相同的顺序）")
    target: dict = Field(description="成片规格 {width,height,fps_num,fps_den}")
    total_outputs: int = Field(description="待产出的成片总数")
    completed_outputs: int = Field(description="已完成的成片数（含失败）")
    failed_outputs: int = Field(description="失败的成片数")
    skipped_outputs: int = Field(description="因取消或依赖片段失败而跳过的成片数")
    total_clips: int = Field(description="需要归一化的片段总数（去重后）")
    done_clips: int = Field(description="已归一化完成的片段数（含失败）")
    current_index: int = Field(description="当前处理的成片序号（0 表示未开始）")
    current_clip: str = Field(description="当前正在归一化的片段文件名")
    current_phase: str = Field(description="当前阶段：normalize / concat，空表示未开始")
    progress_percent: float = Field(description="当前阶段内部的完成百分比（0-100）")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")
    error_message: str = Field(description="失败原因汇总")
    remark: str = Field(description="备注（用户可编辑，最多 200 字；空串表示没写）")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")
    outputs: List[MixOutputItemResponse] = Field(
        default_factory=list, description="每条成片的结果；列表接口不返回明细"
    )

    @field_serializer("started_at", "finished_at")
    def _serialize_optional_times(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value) if value is not None else None

    @classmethod
    def from_model(
        cls, model: MixJob, *, include_outputs: bool = True
    ) -> "MixJobResponse":
        """由 ORM 对象构造响应模型。"""
        return cls(
            id=model.id,
            status=model.status,
            opening=list(model.opening or []),
            middle=list(model.middle or []),
            ending=list(model.ending or []),
            count=model.count,
            output_dir=model.output_dir,
            seed=model.seed,
            target=dict(model.target or {}),
            total_outputs=model.total_outputs,
            completed_outputs=model.completed_outputs,
            failed_outputs=model.failed_outputs,
            skipped_outputs=model.skipped_outputs,
            total_clips=model.total_clips,
            done_clips=model.done_clips,
            current_index=model.current_index,
            current_clip=model.current_clip,
            current_phase=model.current_phase,
            progress_percent=model.progress_percent,
            started_at=model.started_at,
            finished_at=model.finished_at,
            error_message=model.error_message,
            remark=model.remark,
            created_at=model.created_at,
            updated_at=model.updated_at,
            outputs=(
                [MixOutputItemResponse.from_model(item, model.id) for item in model.outputs]
                if include_outputs
                else []
            ),
        )


class MixJobListData(BaseModel):
    """任务列表数据。"""

    total: int = Field(description="满足条件的总条数")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页条数")
    items: List[MixJobResponse] = Field(description="当前页数据")
