"""镜头分割任务相关的 Pydantic 模型。

包含四组：
1. 模板与环境自检 —— 前端用来渲染模板卡片和依赖告警；
2. 任务创建 —— 输入/输出路径、模板与参数覆盖；
3. 任务响应 —— 进度与每个视频的结果；
4. 文件系统浏览 —— 浏览器拿不到本地绝对路径，只能由后端列目录。

路径一律要求绝对路径：后端的 cwd 与用户敲命令时的 cwd 不是一回事，
相对路径会指向一个用户完全没预期的地方，所以宁可 422 也不猜。
"""

import os
from datetime import datetime
from typing import List, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from app.core.scene_templates import (
    CUSTOM_TEMPLATE_KEY,
    DETECTOR_DEFAULT_THRESHOLDS,
    DETECTORS,
    MAX_MIN_LEN,
    MAX_THRESHOLD,
    MIN_MIN_LEN,
    MIN_THRESHOLD,
    TEMPLATE_MAP,
)
from app.models.scene_job import SceneJob, SceneJobItem, SceneJobMode
from app.schemas.common import TimestampMixin, to_utc_iso


def _absolutize(value: str, field: str) -> str:
    """展开 ~ 并规范化为绝对路径；相对路径直接判非法。

    用 os.path.abspath 而不是 Path.resolve()：前者只做词法规范化，
    不会去解析符号链接（macOS 上 /tmp 会被 resolve 成 /private/tmp，
    会让用户看到自己没输入过的路径）。
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} 不能为空")
    expanded = os.path.expanduser(cleaned)
    if not os.path.isabs(expanded):
        raise ValueError(f"{field} 必须是绝对路径（以 / 开头），当前为：{cleaned}")
    return os.path.abspath(expanded)


# --------------------------------------------------------------------------
# 模板与环境自检
# --------------------------------------------------------------------------


class SceneTemplateResponse(BaseModel):
    """一个镜头分割预设模板。"""

    key: str = Field(description="模板标识")
    name: str = Field(description="模板名称")
    summary: str = Field(description="一句话参数说明")
    best_for: str = Field(description="适用场景")
    detector: str = Field(description="检测器标识")
    detector_label: str = Field(description="检测器中文名")
    threshold: Optional[float] = Field(default=None, description="检测阈值，为空表示用默认值")
    threshold_label: str = Field(description="阈值的展示文案，如「默认(3.0)」")
    min_len: float = Field(description="最短镜头秒数")
    copy_mode: bool = Field(description="是否直接复制流不重编码")
    recommended: bool = Field(description="是否为推荐模板")


class DependencyStatus(BaseModel):
    """单个外部依赖的探测结果。"""

    name: str = Field(description="依赖名称")
    ok: bool = Field(description="是否可用")
    path: str = Field(default="", description="解析到的可执行文件路径")
    detail: str = Field(default="", description="探测说明")
    fix_hint: str = Field(default="", description="不可用时的修复命令，直接展示给用户")


class SceneEnvironmentResponse(BaseModel):
    """镜头分割功能的运行环境自检结果。"""

    ready: bool = Field(description="全部依赖是否就绪")
    vct_path: str = Field(description="后端实际调用的 vct 路径")
    vct_exists: bool = Field(description="该路径是否存在且可执行")
    dependencies: List[DependencyStatus] = Field(description="各依赖的探测明细")


# --------------------------------------------------------------------------
# 任务创建
# --------------------------------------------------------------------------


class SceneJobCreate(BaseModel):
    """创建镜头分割任务。"""

    # copy 字段以别名对外（JSON 键仍叫 copy），避免遮蔽 BaseModel.copy 方法
    model_config = ConfigDict(populate_by_name=True)

    input_path: str = Field(
        ..., max_length=1000, description="输入视频文件或目录（绝对路径，支持 ~ 开头）"
    )
    output_dir: Optional[str] = Field(
        default=None, max_length=1000, description="输出根目录，split 模式必填"
    )
    mode: str = Field(
        default=SceneJobMode.SPLIT,
        description=f"任务模式：{' / '.join(SceneJobMode.ALL)}。preview 只检测切点不写文件",
    )
    template: Optional[str] = Field(
        default=None, description="模板标识；custom 或留空表示完全按下面的参数来"
    )
    detector: Optional[str] = Field(
        default=None, description=f"检测器：{' / '.join(DETECTORS)}。留空则用模板值"
    )
    threshold: Optional[float] = Field(
        default=None,
        ge=MIN_THRESHOLD,
        le=MAX_THRESHOLD,
        description="检测阈值，越小越敏感。留空则用模板值或检测器默认值",
    )
    min_len: Optional[float] = Field(
        default=None, ge=MIN_MIN_LEN, le=MAX_MIN_LEN, description="最短镜头秒数，留空则用模板值"
    )
    copy_mode: Optional[bool] = Field(
        default=None,
        alias="copy",
        description="切割时直接复制流不重编码：快且无损，但片段起点只能落在关键帧上",
    )
    recursive: bool = Field(default=False, description="输入为目录时是否递归子目录")
    files: Optional[List[str]] = Field(
        default=None, description="只处理目录下的这些文件名（由前端从列表里勾选），留空表示全部"
    )

    @field_validator("input_path")
    @classmethod
    def _check_input_path(cls, value: str) -> str:
        return _absolutize(value, "输入路径")

    @field_validator("output_dir")
    @classmethod
    def _check_output_dir(cls, value: Optional[str]) -> Optional[str]:
        return _absolutize(value, "输出目录") if value is not None else None

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, value: str) -> str:
        if value not in SceneJobMode.ALL:
            raise ValueError(f"任务模式必须是 {' / '.join(SceneJobMode.ALL)} 之一：{value}")
        return value

    @field_validator("template")
    @classmethod
    def _check_template(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == CUSTOM_TEMPLATE_KEY:
            return value
        if value not in TEMPLATE_MAP:
            raise ValueError(
                f"未知模板：{value}（可选：{', '.join(TEMPLATE_MAP)}、{CUSTOM_TEMPLATE_KEY}）"
            )
        return value

    @field_validator("detector")
    @classmethod
    def _check_detector(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in DETECTORS:
            raise ValueError(f"未知检测器：{value}（可选：{', '.join(DETECTORS)}）")
        return value

    @field_validator("files")
    @classmethod
    def _check_files(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        """勾选的文件只能是文件名，不能带路径分隔符。

        这些名字会与输入目录拼接，允许路径分隔符就等于允许越出输入目录。
        """
        if value is None:
            return None
        result: List[str] = []
        seen: set = set()
        for name in value:
            cleaned = name.strip()
            if not cleaned:
                continue
            if os.sep in cleaned or (os.altsep and os.altsep in cleaned):
                raise ValueError(f"文件名不能包含路径分隔符：{name}")
            if cleaned in seen:
                continue
            seen.add(cleaned)
            result.append(cleaned)
        return result or None

    @model_validator(mode="after")
    def _check_output_required(self) -> "SceneJobCreate":
        """切分模式必须给输出目录，否则切出来的文件没地方放。"""
        if self.mode == SceneJobMode.SPLIT and not self.output_dir:
            raise ValueError("切分模式必须指定输出目录")
        return self


# --------------------------------------------------------------------------
# 任务响应
# --------------------------------------------------------------------------


class SceneJobItemResponse(BaseModel):
    """任务中单个视频的处理结果。"""

    id: int = Field(description="主键")
    index: int = Field(description="在任务内的序号，从 1 开始")
    source_path: str = Field(description="源视频绝对路径")
    source_name: str = Field(description="源视频文件名")
    output_dir: str = Field(description="该视频的输出目录")
    status: str = Field(description="处理状态")
    scene_count: int = Field(description="检测出的镜头数")
    scenes: Optional[list] = Field(default=None, description="切点清单，每项含 number/start/end/duration")
    clip_count: int = Field(description="实际切出的片段数")
    clip_names: List[str] = Field(description="切出的片段文件名")
    failed_clip_count: int = Field(description="切割失败的片段数")
    single_shot: bool = Field(description="是否为单镜头视频（零片段是正常结果）")
    duration_seconds: Optional[float] = Field(default=None, description="源视频时长（秒）")
    exit_code: Optional[int] = Field(default=None, description="子进程退出码")
    elapsed_seconds: int = Field(description="该视频耗时（秒）")
    error_message: str = Field(description="失败原因")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")

    @field_serializer("started_at", "finished_at")
    def _serialize_optional_times(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value) if value is not None else None

    @classmethod
    def from_model(cls, model: SceneJobItem) -> "SceneJobItemResponse":
        """由 ORM 对象构造响应模型。"""
        return cls(
            id=model.id,
            index=model.index,
            source_path=model.source_path,
            source_name=model.source_name,
            output_dir=model.output_dir,
            status=model.status,
            scene_count=model.scene_count,
            scenes=model.scenes,
            clip_count=model.clip_count,
            clip_names=list(model.clip_names or []),
            failed_clip_count=model.failed_clip_count,
            single_shot=model.single_shot,
            duration_seconds=model.duration_seconds,
            exit_code=model.exit_code,
            elapsed_seconds=model.elapsed_seconds,
            error_message=model.error_message,
            started_at=model.started_at,
            finished_at=model.finished_at,
        )


class SceneJobResponse(TimestampMixin):
    """镜头分割任务详情。"""

    id: int = Field(description="主键")
    mode: str = Field(description="任务模式")
    status: str = Field(description="任务状态")
    input_path: str = Field(description="输入路径")
    output_dir: str = Field(description="输出根目录")
    recursive: bool = Field(description="是否递归子目录")
    params: dict = Field(description="实际生效的检测参数")
    total_videos: int = Field(description="待处理视频总数")
    completed_videos: int = Field(description="已处理完成的视频数")
    failed_videos: int = Field(description="处理失败的视频数")
    skipped_videos: int = Field(description="因取消而跳过的视频数")
    clip_count: int = Field(description="已切出的片段总数")
    scene_count: int = Field(description="已检测出的镜头总数")
    current_index: int = Field(description="当前处理的视频序号")
    current_video: str = Field(description="当前处理的视频文件名")
    current_clips: int = Field(description="当前视频已切出的片段数")
    current_clip_names: List[str] = Field(description="当前视频已切出的片段名（供实时预览）")
    progress_percent: int = Field(description="总进度百分比（按视频条数计算，服务端算好）")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")
    error_message: str = Field(description="失败原因汇总")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")
    items: List[SceneJobItemResponse] = Field(
        default_factory=list, description="每个视频的处理结果；列表接口不返回明细"
    )

    @field_serializer("started_at", "finished_at")
    def _serialize_optional_times(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value) if value is not None else None

    @classmethod
    def from_model(
        cls, model: SceneJob, *, include_items: bool = True
    ) -> "SceneJobResponse":
        """由 ORM 对象构造响应模型。

        Args:
            model: 任务 ORM 对象。
            include_items: 列表接口传 False —— 一条任务可能有几十个视频明细，
                批量列表里带上会让响应体大出一个量级。
        """
        return cls(
            id=model.id,
            mode=model.mode,
            status=model.status,
            input_path=model.input_path,
            output_dir=model.output_dir,
            recursive=model.recursive,
            params=dict(model.params or {}),
            total_videos=model.total_videos,
            completed_videos=model.completed_videos,
            failed_videos=model.failed_videos,
            skipped_videos=model.skipped_videos,
            clip_count=model.clip_count,
            scene_count=model.scene_count,
            current_index=model.current_index,
            current_video=model.current_video,
            current_clips=model.current_clips,
            current_clip_names=list(model.current_clip_names or []),
            progress_percent=_progress_percent(model),
            started_at=model.started_at,
            finished_at=model.finished_at,
            error_message=model.error_message,
            created_at=model.created_at,
            updated_at=model.updated_at,
            items=(
                [SceneJobItemResponse.from_model(item) for item in model.items]
                if include_items
                else []
            ),
        )


def _progress_percent(model: SceneJob) -> int:
    """按视频条数算总进度。

    为什么不按片段数：整个任务要切多少个片段，在 vct 检测完成前根本拿不到
    （检测和切割在同一次调用里），硬编一个分母就是假装精确。视频条数则是
    创建任务时就确定的，这个百分比是真的。
    """
    if model.status in ("success", "partial"):
        return 100
    if not model.total_videos:
        return 0
    percent = int(round(100 * model.completed_videos / model.total_videos))
    return max(0, min(100, percent))


class SceneJobListData(BaseModel):
    """任务列表数据。"""

    total: int = Field(description="满足条件的总条数")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页条数")
    items: List[SceneJobResponse] = Field(description="当前页数据")


class SceneClipResponse(BaseModel):
    """切分产出的一段视频，用于结果缩略图网格。"""

    index: int = Field(description="片段序号，从 1 开始")
    name: str = Field(description="片段文件名")
    source_name: str = Field(description="来源视频文件名")
    size_bytes: int = Field(description="文件大小（字节）")
    thumb_url: str = Field(description="缩略图地址（首次访问时后端才抽帧生成）")


# --------------------------------------------------------------------------
# 文件系统浏览
# --------------------------------------------------------------------------


class FsEntry(BaseModel):
    """目录中的一项。"""

    name: str = Field(description="名称")
    path: str = Field(description="绝对路径")
    is_dir: bool = Field(description="是否为目录")
    is_video: bool = Field(description="是否为可处理的视频文件（按后端配置的扩展名判断）")
    size_bytes: Optional[int] = Field(default=None, description="文件大小，目录为空")


class FsListData(BaseModel):
    """列目录结果。"""

    path: str = Field(description="当前目录绝对路径")
    parent: Optional[str] = Field(default=None, description="上级目录，已在根目录时为空")
    entries: List[FsEntry] = Field(description="目录内容，目录在前、同类按名称排序")
    truncated: bool = Field(description="条目是否因过多而被截断")
    video_count: int = Field(description="当前目录下（不含子目录）的视频文件数")


class SceneJobScenesData(BaseModel):
    """预览模式下所有视频的切点汇总。"""

    job_id: int = Field(description="任务 ID")
    status: str = Field(description="任务状态")
    total_scenes: int = Field(description="镜头总数")
    total_duration: float = Field(description="所有视频的镜头总时长（秒）")
    shortest: Optional[float] = Field(default=None, description="最短镜头时长（秒）")
    longest: Optional[float] = Field(default=None, description="最长镜头时长（秒）")
    average: Optional[float] = Field(default=None, description="平均镜头时长（秒）")
    items: List[SceneJobItemResponse] = Field(description="按视频分组的切点明细")
