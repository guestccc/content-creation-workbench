"""一键换背景相关的 Pydantic 模型。

包含三组：
1. 任务创建 —— 原图输入、背景图、输出目录与算法参数；
2. 任务响应 —— 进度与每张原图的结果（含诊断统计）；
3. 任务列表。

路径一律要求绝对路径（理由见 schemas/common.absolutize_path）。

**参数为什么用嵌套模型而不是一个裸 dict**：抠图参数有十几个，范围错一个
（比如 scale 填 0、opacity 填 2）算法不会报错，只会安静地产出一张废图。
在入口处把范围钉死，用户拿到的是一句 422，而不是一张看不出问题的 PNG。
"""

import os
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

from app.models.background_job import BackgroundJob, BackgroundJobItem
from app.schemas.common import TimestampMixin, absolutize_path, to_utc_iso

#: 贴合位置的可选值：中心 / 自动找落点 / 四个角。除此之外只接受 "x,y" 坐标。
POS_KEYWORDS = ("center", "auto", "tl", "tr", "bl", "br")


class BackgroundJobParams(BaseModel):
    """一套抠图 + 贴合参数。默认值全部等于 `抠图.py` 的命令行默认值。

    字段含义与每一步在堵什么漏，见 services/cutout.py 的模块 docstring ——
    这里只负责范围校验，不重复解释算法。
    """

    # ---------- 抠图 ----------
    hi_frac: float = Field(
        default=0.90, ge=0.0, le=1.0,
        description="纸的透明线（相对纸–墨跨度）：亮度高于此比例的像素判为纯纸，透明度 0",
    )
    lo_frac: float = Field(
        default=0.15, ge=0.0, le=1.0,
        description="墨的实心线：亮度低于此比例的像素判为纯墨，透明度 1",
    )
    dark_frac: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="主体定位线档位；留空 = auto（扫档位取包围盒炸开前那一档）",
    )
    pedestal: float = Field(
        default=0.08, ge=0.0, le=1.0,
        description="纸纹截断：低于此透明度的淡灰直接归零，否则整张蒙雾",
    )
    gate: bool = Field(default=True, description="位置门：主体包围盒之外整片置零")
    gate_pad: float = Field(
        default=0.12, ge=0.0, le=1.0, description="位置门往外扩的比例"
    )
    warm: bool = Field(
        default=True, description="暖色抑制：按彩度压掉主体旁边的木纹 / 暖色桌面"
    )
    keep_color: bool = Field(
        default=False, description="保留原始像素颜色（默认统一成墨色，避免深色底上出白边）"
    )

    # ---------- 贴合 ----------
    # 默认 0.1 而不是 None（原尺寸）：手绘原图动辄 1080×1440，随手挑的背景常常比它
    # 小，原尺寸贴会直接判失败（见 services/cutout.py 的「贴纸比背景大」）。给个能
    # 放得下的默认值，用户不改参数就能出一批能看的图，嫌小再往上调。
    # 留空（null）仍然有效 = 不缩放、按原尺寸居中贴，没把这个能力删掉。
    scale: Optional[float] = Field(
        default=0.1, gt=0.0, le=10.0,
        description="贴纸宽度占底图宽度的比例（默认 0.1，即占底图宽的 10%）；留空 = 不缩放，按原尺寸居中贴",
    )
    pos: Optional[str] = Field(
        default=None,
        max_length=32,
        description=f"贴合位置：{' / '.join(POS_KEYWORDS)} 或 \"x,y\" 坐标；留空 = center",
    )
    search_from: float = Field(
        default=0.35, ge=0.0, le=1.0,
        description="自动落点的搜索起点（距页顶的比例），pos=auto 时生效",
    )
    margin: int = Field(
        default=20, ge=0, le=10000, description="贴纸离底图边缘的最小距离（像素）"
    )
    rotate: float = Field(
        default=0.0, ge=-360.0, le=360.0, description="贴纸旋转角度（度）"
    )
    opacity: float = Field(default=1.0, ge=0.0, le=1.0, description="贴纸不透明度 0–1")

    @field_validator("pos")
    @classmethod
    def _check_pos(cls, value: Optional[str]) -> Optional[str]:
        """位置只认关键字或 "x,y"。

        拼错的关键字（比如 "centre"）算法会静默退回默认行为，用户以为生效了
        其实没有 —— 那种「点了没用还不报错」最难查，所以在入口拦掉。
        """
        if value is None:
            return None
        cleaned = value.strip().lower()
        if not cleaned:
            return None
        if cleaned in POS_KEYWORDS:
            return cleaned
        parts = cleaned.split(",")
        if len(parts) == 2:
            try:
                int(parts[0].strip()), int(parts[1].strip())
            except ValueError:
                pass
            else:
                return cleaned
        raise ValueError(
            f"贴合位置只能是 {' / '.join(POS_KEYWORDS)} 之一，或 \"x,y\" 坐标：{value}"
        )

    @model_validator(mode="after")
    def _check_bands(self) -> "BackgroundJobParams":
        """纸的透明线必须高于墨的实心线。

        反了的话亮度映射的方向就倒过来了 —— 算法不会报错，只会把纸抠成墨、
        墨抠成纸，产出一张「背景保留、主体透明」的反图。
        """
        if self.hi_frac <= self.lo_frac:
            raise ValueError(
                f"纸的透明线（{self.hi_frac}）必须大于墨的实心线（{self.lo_frac}）"
            )
        return self


class BackgroundJobCreate(BaseModel):
    """创建一键换背景任务。"""

    input_path: str = Field(
        ..., max_length=1000, description="原图文件或目录（绝对路径，支持 ~ 开头）"
    )
    background_path: str = Field(
        ..., max_length=1000, description="背景图（绝对路径，支持 ~ 开头）"
    )
    output_dir: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="产物输出目录（绝对路径）；留空用 materials/background 下的新子目录",
    )
    files: Optional[List[str]] = Field(
        default=None,
        description="只处理输入目录下的这些文件名（由前端从列表里勾选），留空表示全部图片",
    )
    params: BackgroundJobParams = Field(
        default_factory=BackgroundJobParams,
        description="算法参数（默认值 = 脚本默认，只有缩放比例为 0.1 是有意改过的）",
    )

    @field_validator("input_path")
    @classmethod
    def _check_input_path(cls, value: str) -> str:
        return absolutize_path(value, "原图输入路径")

    @field_validator("background_path")
    @classmethod
    def _check_background_path(cls, value: str) -> str:
        return absolutize_path(value, "背景图路径")

    @field_validator("output_dir")
    @classmethod
    def _check_output_dir(cls, value: Optional[str]) -> Optional[str]:
        return absolutize_path(value, "输出目录") if value is not None else None

    @field_validator("files")
    @classmethod
    def _check_files(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        """勾选的文件只能是文件名，不能带路径分隔符。

        这些名字会与输入目录拼接，允许路径分隔符就等于允许越出输入目录。
        （与 subtitle_job.SubtitleJobCreate._check_files 同一套理由，见那里。）
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


# --------------------------------------------------------------------------
# 任务响应
# --------------------------------------------------------------------------


class BackgroundJobItemResponse(BaseModel):
    """任务中单张原图的处理结果。"""

    id: int = Field(description="主键")
    index: int = Field(description="在任务内的序号，从 1 开始")
    source_path: str = Field(description="源原图绝对路径")
    source_name: str = Field(description="源原图文件名")
    output_path: str = Field(description="产物 PNG 路径")
    output_name: str = Field(description="产物文件名")
    status: str = Field(description="处理状态")
    stats: dict = Field(
        default_factory=dict,
        description=(
            "诊断统计：纸色 / 墨色 / 跨度 / 定位线档位与自动选择说明 / 位置门与暖色"
            "抑制各抠掉多少像素 / 可见占比 / warnings。这个算法的失败模式光看产物图"
            "很难判断，这组数字是排查的主要依据"
        ),
    )
    width: int = Field(description="产物宽度（像素）")
    height: int = Field(description="产物高度（像素）")
    size_bytes: int = Field(description="产物文件大小（字节）")
    elapsed_seconds: int = Field(description="该张耗时（秒）")
    error_message: str = Field(description="失败原因")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")

    @field_serializer("started_at", "finished_at")
    def _serialize_optional_times(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value) if value is not None else None

    @classmethod
    def from_model(cls, model: BackgroundJobItem) -> "BackgroundJobItemResponse":
        """由 ORM 对象构造响应模型。"""
        return cls(
            id=model.id,
            index=model.index,
            source_path=model.source_path,
            source_name=model.source_name,
            output_path=model.output_path,
            output_name=os.path.basename(model.output_path),
            status=model.status,
            stats=dict(model.stats or {}),
            width=model.width,
            height=model.height,
            size_bytes=model.size_bytes,
            elapsed_seconds=model.elapsed_seconds,
            error_message=model.error_message,
            started_at=model.started_at,
            finished_at=model.finished_at,
        )


class BackgroundJobResponse(TimestampMixin):
    """一键换背景任务详情。"""

    id: int = Field(description="主键")
    status: str = Field(description="任务状态")
    input_path: str = Field(description="原图输入路径")
    output_dir: str = Field(description="产物输出目录")
    files: List[str] = Field(description="勾选的原图文件名清单；为空表示处理目录下全部")
    background_path: str = Field(description="背景图绝对路径")
    background_name: str = Field(description="背景图文件名")
    params: dict = Field(description="实际生效的算法参数")
    total_images: int = Field(description="待处理原图总数")
    completed_images: int = Field(description="已处理完成的原图数")
    failed_images: int = Field(description="处理失败的原图数")
    skipped_images: int = Field(description="因取消而跳过的原图数")
    current_index: int = Field(description="当前处理的原图序号")
    current_image: str = Field(description="当前处理的原图文件名")
    current_elapsed_seconds: int = Field(
        description="当前这张已跑的秒数。抠图没有中间进度可上报，页面只能如实显示已用时"
    )
    progress_percent: int = Field(description="总进度百分比（按张数计算，服务端算好）")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")
    error_message: str = Field(description="失败原因汇总")
    remark: str = Field(description="备注（用户可编辑，最多 200 字；空串表示没写）")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")
    items: List[BackgroundJobItemResponse] = Field(
        default_factory=list, description="每张原图的处理结果；列表接口不返回明细"
    )

    @field_serializer("started_at", "finished_at")
    def _serialize_optional_times(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value) if value is not None else None

    @classmethod
    def from_model(
        cls, model: BackgroundJob, *, include_items: bool = True
    ) -> "BackgroundJobResponse":
        """由 ORM 对象构造响应模型。

        Args:
            model: 任务 ORM 对象。
            include_items: 列表接口传 False —— 一条任务可能有几百条明细，
                批量列表里带上会让响应体大出一个量级。
        """
        return cls(
            id=model.id,
            status=model.status,
            input_path=model.input_path,
            output_dir=model.output_dir,
            files=list(model.files or []),
            background_path=model.background_path,
            background_name=os.path.basename(model.background_path),
            params=dict(model.params or {}),
            total_images=model.total_images,
            completed_images=model.completed_images,
            failed_images=model.failed_images,
            skipped_images=model.skipped_images,
            current_index=model.current_index,
            current_image=model.current_image,
            current_elapsed_seconds=model.current_elapsed_seconds,
            progress_percent=_progress_percent(model),
            started_at=model.started_at,
            finished_at=model.finished_at,
            error_message=model.error_message,
            remark=model.remark,
            created_at=model.created_at,
            updated_at=model.updated_at,
            items=(
                [BackgroundJobItemResponse.from_model(item) for item in model.items]
                if include_items
                else []
            ),
        )


def _progress_percent(model: BackgroundJob) -> int:
    """按图片张数算总进度。

    为什么不按时间：抠图是纯 numpy 运算，一张图要多久只有跑完才知道，
    而且和图片尺寸强相关（一张 4000×3000 的能顶十张小图）。图片张数则是
    创建任务时就确定的，与 subtitle 那边「分母只认真实已知的量」同一个取舍。
    """
    if model.status in ("success", "partial"):
        return 100
    if not model.total_images:
        return 0
    percent = int(round(100 * model.completed_images / model.total_images))
    return max(0, min(100, percent))


class BackgroundJobListData(BaseModel):
    """任务列表数据。"""

    total: int = Field(description="满足条件的总条数")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页条数")
    items: List[BackgroundJobResponse] = Field(description="当前页数据")
