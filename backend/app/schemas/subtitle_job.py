"""字幕提取相关的 Pydantic 模型。

包含四组：
1. 环境自检 —— 是否装了 VideoCaptioner、缺什么、按当前系统怎么装；
2. 任务创建 —— 输入/输出路径与转写参数；
3. 任务响应 —— 进度与每条视频的结果；
4. 字幕产物 —— 产物清单与单个 .srt 的文本预览。

路径一律要求绝对路径（理由见 schemas/common.absolutize_path）。
"""

import os
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer, field_validator

from app.core.subtitle_asr import ASR_ENGINE_MAP, engines_payload
from app.models.subtitle_job import SubtitleJob, SubtitleJobItem
from app.schemas.common import TimestampMixin, absolutize_path, to_utc_iso

#: 本期只出 srt。VideoCaptioner 还支持 ass / txt / json，但页面上的需求是
#: 「把字幕导出来」，srt 是唯一一种所有下游（剪辑软件、文本工具）都认的格式。
OUTPUT_FORMAT = "srt"


class AsrEngineResponse(BaseModel):
    """一个 ASR 引擎选项（真源在后端，见 core/subtitle_asr.py）。"""

    key: str = Field(description="引擎标识（传给后端的值）")
    name: str = Field(description="引擎名称")
    summary: str = Field(description="一句话说明")
    requires_key: bool = Field(description="是否需要先在 VideoCaptioner 里配好 key")
    recommended: bool = Field(description="是否推荐（页面上默认选中）")


class InstallHint(BaseModel):
    """一条安装指引。命令只是文本，供前端展示与复制。"""

    title: str = Field(description="这一步做什么")
    command: str = Field(default="", description="要执行的命令；为空表示这一步没有命令")
    note: str = Field(default="", description="补充说明")
    url: str = Field(default="", description="相关链接")


class SubtitleEnvironmentResponse(BaseModel):
    """字幕提取功能的运行环境自检结果。"""

    installed: bool = Field(description="是否探测到可用的 VideoCaptioner")
    ready: bool = Field(description="能否开始转写：装了 VideoCaptioner 且 ffmpeg 可用")
    launcher: List[str] = Field(description="后端实际调用的命令前缀（诊断用）")
    kind: str = Field(description="命中方式：override / venv-python / backend-python / path 等")
    root: str = Field(description="探测时使用的 VideoCaptioner 根目录")
    version: str = Field(description="VideoCaptioner 版本，拿不到为空")
    python_version: str = Field(description="目标解释器的 Python 版本")
    config_file: str = Field(description="VideoCaptioner 配置文件路径（平台决定，见下）")
    config_exists: bool = Field(description="配置文件是否已存在")
    ffmpeg_path: str = Field(description="ffmpeg 路径；为空表示没找到，转写会失败")
    detail: str = Field(description="未安装时的原因说明")
    platform: str = Field(description="后端所在系统：macos / windows / linux")
    platform_label: str = Field(description="系统名的中文展示")
    python_platform: str = Field(description="platform.platform() 的原始输出，排查用")
    materials_dir: str = Field(
        description="素材目录根（仓库根目录的 materials/），下有 source/clips/subtitle/output 四个分段"
    )
    default_input_dir: str = Field(
        description="默认输入目录（materials/source），页面用它作为输入目录的初始值"
    )
    default_output_dir: str = Field(
        description="默认输出目录（materials/subtitle），页面用它作为输出目录的初始值"
    )
    install_hints: List[InstallHint] = Field(description="按当前系统给出的安装指引")
    asr_engines: List[AsrEngineResponse] = Field(description="可选的 ASR 引擎")


class SubtitleJobCreate(BaseModel):
    """创建字幕提取任务。"""

    input_path: str = Field(
        ..., max_length=1000, description="输入视频文件或目录（绝对路径，支持 ~ 开头）"
    )
    output_dir: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="字幕输出目录（绝对路径）；留空用 materials/subtitle",
    )
    asr: Optional[str] = Field(
        default=None,
        description=f"ASR 引擎：{' / '.join(ASR_ENGINE_MAP)}。留空用默认（bijian）",
    )
    language: Optional[str] = Field(
        default=None,
        max_length=16,
        description="识别语言（ISO 639-1），留空或 auto 表示自动检测",
    )
    recursive: bool = Field(default=False, description="输入为目录时是否递归子目录")
    files: Optional[List[str]] = Field(
        default=None, description="只处理目录下的这些文件名（由前端从列表里勾选），留空表示全部"
    )

    @field_validator("input_path")
    @classmethod
    def _check_input_path(cls, value: str) -> str:
        return absolutize_path(value, "输入路径")

    @field_validator("output_dir")
    @classmethod
    def _check_output_dir(cls, value: Optional[str]) -> Optional[str]:
        return absolutize_path(value, "输出目录") if value is not None else None

    @field_validator("asr")
    @classmethod
    def _check_asr(cls, value: Optional[str]) -> Optional[str]:
        """只放行 CLI 真正认的引擎。

        不在清单里的值直接 422，而不是悄悄换成默认值 —— 用户点了一个选项
        却跑了另一个引擎，比报错更难排查。
        """
        if value is None or value == "":
            return None
        if value not in ASR_ENGINE_MAP:
            raise ValueError(
                f"未知的 ASR 引擎：{value}（可选：{', '.join(ASR_ENGINE_MAP)}）"
            )
        return value

    @field_validator("language")
    @classmethod
    def _check_language(cls, value: Optional[str]) -> Optional[str]:
        """识别语言：留空/auto 表示自动检测，其余只做最基本的形状检查。

        不做语言白名单 —— CLI 接受任意 ISO 639-1 代码，后端拦一道反而是
        自己给自己加戏。只挡住明显不像代码的输入。
        """
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned or cleaned.lower() == "auto":
            return None
        if len(cleaned) > 16 or not cleaned.replace("-", "").isalpha():
            raise ValueError(f"识别语言看起来不是合法的语言代码：{value}")
        return cleaned.lower()

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


# --------------------------------------------------------------------------
# 任务响应
# --------------------------------------------------------------------------


class SubtitleJobItemResponse(BaseModel):
    """任务中单条视频的处理结果。"""

    id: int = Field(description="主键")
    index: int = Field(description="在任务内的序号，从 1 开始")
    source_path: str = Field(description="源视频绝对路径")
    source_name: str = Field(description="源视频文件名")
    output_path: str = Field(description="字幕输出文件路径")
    output_name: str = Field(description="字幕文件名")
    status: str = Field(description="处理状态")
    subtitle_exists: bool = Field(description="字幕文件是否已落盘")
    file_size: int = Field(description="字幕文件大小（字节）")
    segment_count: int = Field(description="字幕条数")
    duration_seconds: Optional[float] = Field(default=None, description="源视频时长（秒）")
    exit_code: Optional[int] = Field(default=None, description="子进程退出码")
    elapsed_seconds: int = Field(description="该条耗时（秒）")
    error_message: str = Field(description="失败原因")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")

    @field_serializer("started_at", "finished_at")
    def _serialize_optional_times(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value) if value is not None else None

    @classmethod
    def from_model(cls, model: SubtitleJobItem) -> "SubtitleJobItemResponse":
        """由 ORM 对象构造响应模型。"""
        return cls(
            id=model.id,
            index=model.index,
            source_path=model.source_path,
            source_name=model.source_name,
            output_path=model.output_path,
            output_name=os.path.basename(model.output_path),
            status=model.status,
            subtitle_exists=model.subtitle_exists,
            file_size=model.file_size,
            segment_count=model.segment_count,
            duration_seconds=model.duration_seconds,
            exit_code=model.exit_code,
            elapsed_seconds=model.elapsed_seconds,
            error_message=model.error_message,
            started_at=model.started_at,
            finished_at=model.finished_at,
        )


class SubtitleJobResponse(TimestampMixin):
    """字幕提取任务详情。"""

    id: int = Field(description="主键")
    status: str = Field(description="任务状态")
    input_path: str = Field(description="输入路径")
    output_dir: str = Field(description="字幕输出目录")
    recursive: bool = Field(description="是否递归子目录")
    params: dict = Field(description="实际生效的转写参数")
    total_videos: int = Field(description="待处理视频总数")
    completed_videos: int = Field(description="已处理完成的视频数")
    failed_videos: int = Field(description="处理失败的视频数")
    skipped_videos: int = Field(description="因取消而跳过的视频数")
    subtitle_count: int = Field(description="已产出的字幕文件数")
    current_index: int = Field(description="当前处理的视频序号")
    current_video: str = Field(description="当前处理的视频文件名")
    current_elapsed_seconds: int = Field(
        description="当前这条已跑的秒数。转写拿不到机器可读的百分比，页面只能如实显示已用时"
    )
    progress_percent: int = Field(description="总进度百分比（按视频条数计算，服务端算好）")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")
    error_message: str = Field(description="失败原因汇总")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")
    items: List[SubtitleJobItemResponse] = Field(
        default_factory=list, description="每条视频的处理结果；列表接口不返回明细"
    )

    @field_serializer("started_at", "finished_at")
    def _serialize_optional_times(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value) if value is not None else None

    @classmethod
    def from_model(
        cls, model: SubtitleJob, *, include_items: bool = True
    ) -> "SubtitleJobResponse":
        """由 ORM 对象构造响应模型。

        Args:
            model: 任务 ORM 对象。
            include_items: 列表接口传 False —— 一条任务可能有几十条明细，
                批量列表里带上会让响应体大出一个量级。
        """
        return cls(
            id=model.id,
            status=model.status,
            input_path=model.input_path,
            output_dir=model.output_dir,
            recursive=model.recursive,
            params=dict(model.params or {}),
            total_videos=model.total_videos,
            completed_videos=model.completed_videos,
            failed_videos=model.failed_videos,
            skipped_videos=model.skipped_videos,
            subtitle_count=model.subtitle_count,
            current_index=model.current_index,
            current_video=model.current_video,
            current_elapsed_seconds=model.current_elapsed_seconds,
            progress_percent=_progress_percent(model),
            started_at=model.started_at,
            finished_at=model.finished_at,
            error_message=model.error_message,
            created_at=model.created_at,
            updated_at=model.updated_at,
            items=(
                [SubtitleJobItemResponse.from_model(item) for item in model.items]
                if include_items
                else []
            ),
        )


def _progress_percent(model: SubtitleJob) -> int:
    """按视频条数算总进度。

    为什么不按时间：一条视频要转写多久，在它跑完之前无从得知
    （VideoCaptioner 的进度条只在终端里渲染，重定向到文件后没有任何可解析的
    输出），硬编一个分母就是假装精确。视频条数则是创建任务时就确定的。
    """
    if model.status in ("success", "partial"):
        return 100
    if not model.total_videos:
        return 0
    percent = int(round(100 * model.completed_videos / model.total_videos))
    return max(0, min(100, percent))


class SubtitleJobListData(BaseModel):
    """任务列表数据。"""

    total: int = Field(description="满足条件的总条数")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页条数")
    items: List[SubtitleJobResponse] = Field(description="当前页数据")


class SubtitleFileResponse(BaseModel):
    """一份产出的字幕文件。"""

    index: int = Field(description="序号，从 1 开始（全任务范围内连续，预览接口用它定位）")
    item_index: int = Field(description="所属视频在任务内的序号，从 1 开始")
    name: str = Field(description="字幕文件名")
    source_name: str = Field(description="来源视频文件名")
    size_bytes: int = Field(description="文件大小（字节）")
    segment_count: int = Field(description="字幕条数")


class SubtitleTextData(BaseModel):
    """一份字幕文件的文本内容（给页面预览用）。"""

    index: int = Field(description="序号")
    name: str = Field(description="字幕文件名")
    source_name: str = Field(description="来源视频文件名")
    content: str = Field(description="字幕文本（srt 原文）")
    size_bytes: int = Field(description="文件大小（字节）")
    truncated: bool = Field(description="是否因文件过大而只返回了开头一段")


def engines_payload_response() -> List[AsrEngineResponse]:
    """ASR 清单转成响应模型（环境自检接口用）。"""
    return [AsrEngineResponse(**item) for item in engines_payload()]
