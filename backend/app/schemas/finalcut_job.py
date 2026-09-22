"""一键成品相关的 Pydantic 模型。

四组：
1. AI 产物契约 —— analysis / copies 的形状，既是 finalcut_copy.parse_copy_payload
   的校验目标，也是接口响应里 result 字段的形状（前后端共用同一份定义）；
2. 任务创建 —— 文案任务（字幕 + 成片 + 条数 + 提示）与合成任务（文案快照 × 框 × 样式）；
3. 任务响应 —— 进度与每条成片的结果；
4. 环境与设置 —— 环境自检、AI 配置读写。

任意本地文件的路径一律在创建请求里校验（绝对路径 + 后缀白名单），
固化进任务记录后接口只认记录里的路径 —— 与混剪「注册素材目录」的先例一致。
"""

from datetime import datetime
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer, field_validator

from app.core.config import settings
from app.models.finalcut_job import FinalcutCopyJob, FinalcutRenderItem, FinalcutRenderJob
from app.schemas.common import TimestampMixin, absolutize_path, to_utc_iso
from app.services.finalcut_render import TEXT_STYLES
from app.services.finalcut_settings import normalize_chars_per_second

#: 字幕素材允许的扩展名（小写、带点）。
SUBTITLE_INPUT_EXTENSIONS = (".srt", ".ass", ".vtt")
#: 成片视频允许的扩展名（小写、带点）。
VIDEO_INPUT_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm")


# --------------------------------------------------------------------------
# AI 产物契约
# --------------------------------------------------------------------------


class CopyBreakdownPart(BaseModel):
    """一条文案的逐段拆解中的一段。"""

    part: str = Field(default="", description="段落名（如「开头 3 秒」「行动号召」）")
    content: str = Field(default="", description="该段的文案原文")
    explain: str = Field(default="", description="这段为什么这么写 / 起什么作用")


class CopyCandidate(BaseModel):
    """一条候选广告文案（AI 产物的一条，也是用户勾选/编辑的对象）。"""

    text: str = Field(..., min_length=1, description="文案正文（可含换行）")
    angle: str = Field(default="", description="切入角度（痛点开场/效果对比/价格锚点…）")
    char_count: int = Field(default=0, description="口播字数（服务端实测，供一眼核对）")
    why: str = Field(default="", description="为什么这么写（2-3 句）")
    highlights: List[str] = Field(default_factory=list, description="好在哪里（要点列表）")
    breakdown: List[CopyBreakdownPart] = Field(
        default_factory=list, description="逐段拆解"
    )


class CopyAnalysis(BaseModel):
    """AI 对字幕素材的拆解（生成文案的论据，直接展示给用户）。"""

    topic: str = Field(default="", description="素材主题")
    audience: str = Field(default="", description="目标受众")
    selling_points: List[str] = Field(default_factory=list, description="核心卖点")
    tone: str = Field(default="", description="调性（如「紧迫促销」「闺蜜安利」）")


class CopyResultPayload(BaseModel):
    """一次文案生成的完整产物（落库到 FinalcutCopyJob.result）。"""

    analysis: CopyAnalysis = Field(default_factory=CopyAnalysis)
    copies: List[CopyCandidate] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 任务创建
# --------------------------------------------------------------------------


def _check_extension(path: str, allowed: tuple, field: str) -> str:
    """后缀白名单校验（大小写不敏感）。"""
    suffix = Path(path).suffix.lower()
    if suffix not in allowed:
        raise ValueError(f"{field} 只支持 {' / '.join(allowed)} 文件，当前是：{suffix or '（无扩展名）'}")
    return path


class FinalcutCopyJobCreate(BaseModel):
    """创建文案生成任务。"""

    subtitle_path: str = Field(
        ..., min_length=1, max_length=1000,
        description="字幕素材绝对路径（历史产物或本地 .srt/.ass/.vtt）",
    )
    video_path: str = Field(
        ..., min_length=1, max_length=1000,
        description="成片视频绝对路径（历史成片或本地 .mp4/.mov/.mkv/.webm）",
    )
    copy_count: int = Field(
        default=0,
        # validate_default：0 走校验器换成系统默认条数，默认是不校验默认值的
        validate_default=True,
        description="生成几条候选文案；0 = 用系统默认",
    )
    chars_per_second: Optional[float] = Field(
        default=None,
        description="口播语速（字/秒，1.0–15.0）；不传 = 用当前全局默认值，"
                    "两者都会快照进任务记录",
    )
    hint: str = Field(default="", description="补充提示（卖点/受众/偏好），可空")

    @field_validator("subtitle_path")
    @classmethod
    def _subtitle_abs(cls, value: str) -> str:
        return _check_extension(
            absolutize_path(value, "字幕文件"), SUBTITLE_INPUT_EXTENSIONS, "字幕文件"
        )

    @field_validator("video_path")
    @classmethod
    def _video_abs(cls, value: str) -> str:
        return _check_extension(
            absolutize_path(value, "视频文件"), VIDEO_INPUT_EXTENSIONS, "视频文件"
        )

    @field_validator("copy_count")
    @classmethod
    def _check_count(cls, value: int) -> int:
        if value < 0:
            raise ValueError("文案条数不能为负")
        if value > settings.FINALCUT_COPY_COUNT_MAX:
            raise ValueError(f"一次最多生成 {settings.FINALCUT_COPY_COUNT_MAX} 条文案")
        return value or settings.FINALCUT_COPY_COUNT_DEFAULT

    @field_validator("chars_per_second")
    @classmethod
    def _check_rate(cls, value: Optional[float]) -> Optional[float]:
        """None 直通（= 用全局默认值），给了值就走与配置弹窗同一套区间校验。"""
        if value is None:
            return None
        return normalize_chars_per_second(value)

    @field_validator("hint")
    @classmethod
    def _check_hint(cls, value: str) -> str:
        value = value.strip()
        if len(value) > settings.FINALCUT_HINT_MAX_CHARS:
            raise ValueError(f"补充提示最多 {settings.FINALCUT_HINT_MAX_CHARS} 字")
        return value


class BoxSpec(BaseModel):
    """视频画面上的框选区域（归一化坐标，0-1，相对显示尺寸）。"""

    x: float = Field(..., ge=0.0, le=1.0, description="左上角 x（归一化）")
    y: float = Field(..., ge=0.0, le=1.0, description="左上角 y（归一化）")
    w: float = Field(..., ge=0.02, le=1.0, description="宽（归一化，至少 2% 画面宽）")
    h: float = Field(..., ge=0.02, le=1.0, description="高（归一化，至少 2% 画面高）")

    @field_validator("w")
    @classmethod
    def _check_right_edge(cls, value: float, info) -> float:
        if "x" in info.data and info.data["x"] + value > 1.0 + 1e-6:
            raise ValueError("框的右边缘超出画面（x + w 必须 ≤ 1）")
        return value

    @field_validator("h")
    @classmethod
    def _check_bottom_edge(cls, value: float, info) -> float:
        if "y" in info.data and info.data["y"] + value > 1.0 + 1e-6:
            raise ValueError("框的下边缘超出画面（y + h 必须 ≤ 1）")
        return value


class FinalcutRenderItemSpec(BaseModel):
    """合成任务里的一条：文案快照 × 框 × 样式。"""

    copy_text: str = Field(..., min_length=1, max_length=2000, description="文案正文（用户可改过字）")
    angle: str = Field(default="", max_length=100, description="文案角度（展示用）")
    style: str = Field(default="white_box", description="上屏样式模板 key")
    box: BoxSpec = Field(..., description="框选区域")
    font_size: int = Field(default=0, ge=0, le=200, description="字号；0 = 自动")

    @field_validator("copy_text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("文案不能只含空白字符")
        return value

    @field_validator("style")
    @classmethod
    def _style_known(cls, value: str) -> str:
        if value not in TEXT_STYLES:
            raise ValueError(f"未知的文案样式：{value}（可选：{', '.join(TEXT_STYLES)}）")
        return value


class FinalcutRenderJobCreate(BaseModel):
    """创建合成任务（勾选的若干文案 + 各自的框选与样式）。"""

    video_path: str = Field(
        ..., min_length=1, max_length=1000, description="成片视频绝对路径"
    )
    copy_job_id: Optional[int] = Field(
        default=None, ge=1, description="来源文案任务 id（仅溯源，可空）"
    )
    items: List[FinalcutRenderItemSpec] = Field(
        ..., min_length=1, description="每条候选文案一份（含框选与样式）"
    )
    output_dir: str = Field(
        default="", max_length=1000,
        description="产物目录；空 = materials/finalcut/finalcut-<时间戳>",
    )

    @field_validator("video_path")
    @classmethod
    def _video_abs(cls, value: str) -> str:
        return _check_extension(
            absolutize_path(value, "视频文件"), VIDEO_INPUT_EXTENSIONS, "视频文件"
        )

    @field_validator("items")
    @classmethod
    def _check_item_count(cls, value: List[FinalcutRenderItemSpec]):
        if len(value) > settings.FINALCUT_MAX_ITEMS:
            raise ValueError(f"一次最多合成 {settings.FINALCUT_MAX_ITEMS} 条成片")
        return value

    @field_validator("output_dir")
    @classmethod
    def _output_abs(cls, value: str) -> str:
        value = value.strip()
        return absolutize_path(value, "输出目录") if value else ""


# --------------------------------------------------------------------------
# 任务响应
# --------------------------------------------------------------------------


class FinalcutCopyJobResponse(TimestampMixin):
    """文案生成任务详情。"""

    id: int = Field(description="主键")
    status: str = Field(description="任务状态")
    subtitle_path: str = Field(description="字幕素材绝对路径")
    video_path: str = Field(description="成片视频绝对路径")
    video_duration: float = Field(description="视频时长（秒）")
    chars_per_second: float = Field(
        description="本次任务实际生效的口播语速（字/秒）；0 = 升级前创建的老任务，"
                    "当时按全局值跑的、具体值不可考"
    )
    copy_count: int = Field(description="请求的候选文案条数")
    hint: str = Field(description="用户补充提示")
    model: str = Field(description="实际使用的模型名")
    result: Optional[CopyResultPayload] = Field(
        default=None, description="生成结果（analysis + copies），成功后才有"
    )
    tokens_used: int = Field(description="本次调用的 total_tokens")
    current_phase: str = Field(description="当前相位：read / analyze / parse")
    progress_percent: float = Field(description="完成百分比（0-100）")
    error_message: str = Field(description="失败原因")
    remark: str = Field(description="备注（用户可编辑，最多 200 字；空串表示没写）")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    @field_serializer("started_at", "finished_at")
    def _serialize_optional_times(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value) if value is not None else None

    @classmethod
    def from_model(cls, model: FinalcutCopyJob) -> "FinalcutCopyJobResponse":
        """由 ORM 对象构造响应模型。"""
        result = model.result if isinstance(model.result, dict) else None
        return cls(
            id=model.id,
            status=model.status,
            subtitle_path=model.subtitle_path,
            video_path=model.video_path,
            video_duration=model.video_duration,
            chars_per_second=model.chars_per_second,
            copy_count=model.copy_count,
            hint=model.hint,
            model=model.model,
            result=CopyResultPayload(**result) if result else None,
            tokens_used=model.tokens_used,
            current_phase=model.current_phase,
            progress_percent=model.progress_percent,
            error_message=model.error_message,
            remark=model.remark,
            started_at=model.started_at,
            finished_at=model.finished_at,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class FinalcutCopySubtitleText(BaseModel):
    """一条文案任务的字幕内容（第 ② 步左右对照用：左列看素材，右列看 AI 产物）。

    单独一个端点、而不是塞进任务响应：任务是轮询着拉的，字幕可能有几百 KB，
    塞进去会把轮询打死（与 SubtitleJobResponse.from_model(include_items=False)
    同一考虑）。

    两个视图来自同一次读盘：
    - `content` 是**磁盘上那份字幕的原文**（带序号与时间轴，超上限时截断）；
    - `material` 是**喂给 AI 的素材**（去序号/时间轴、合并连续重复行、剥 ASS
      标签，按 FINALCUT_SRT_MAX_CHARS 截断）—— 前端那份「去时间轴」的文本
      变换与它不等价，别拿前端算的冒充 AI 输入。
    """

    path: str = Field(description="字幕文件绝对路径（取自任务记录，不接受前端传入）")
    name: str = Field(description="文件名")
    size_bytes: int = Field(description="文件大小（字节）")
    content: str = Field(description="字幕原文（超上限时只给开头一段）")
    truncated: bool = Field(
        description="原文是否被截断（文件超过 SUBTITLE_PREVIEW_MAX_BYTES，只显示了开头）"
    )
    material: str = Field(description="喂给 AI 的素材文本")
    material_truncated: bool = Field(
        description=(
            "素材是否被截断（超过 FINALCUT_SRT_MAX_CHARS 上限，"
            "AI 当时也只读到了这些）"
        )
    )


class FinalcutRenderItemResponse(BaseModel):
    """合成任务中的一条成片。"""

    id: int = Field(description="主键")
    index: int = Field(description="成片序号（从 1 开始）")
    status: str = Field(description="该条成片的状态")
    copy_text: str = Field(description="文案快照")
    angle: str = Field(description="文案角度")
    style: str = Field(description="上屏样式模板 key")
    box: dict = Field(description="框选区域 {x,y,w,h}（归一化）")
    font_size: int = Field(description="请求字号（0 = 自动）")
    resolved_font_size: int = Field(description="实际使用的字号")
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
    def from_model(cls, model: FinalcutRenderItem, job_id: int) -> "FinalcutRenderItemResponse":
        """由 ORM 对象构造响应模型，顺手拼上媒体 URL。"""
        base = f"/api/v1/finalcut/render-jobs/{job_id}/outputs/{model.index}"
        has_output = model.status == "success" and bool(model.output_path)
        return cls(
            id=model.id,
            index=model.index,
            status=model.status,
            copy_text=model.copy_text,
            angle=model.angle,
            style=model.style,
            box=dict(model.box or {}),
            font_size=model.font_size,
            resolved_font_size=model.resolved_font_size,
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


class FinalcutRenderJobResponse(TimestampMixin):
    """合成任务详情。"""

    id: int = Field(description="主键")
    status: str = Field(description="任务状态")
    copy_job_id: Optional[int] = Field(default=None, description="来源文案任务 id")
    video_path: str = Field(description="成片视频绝对路径")
    video_duration: float = Field(description="视频时长（秒）")
    video_spec: dict = Field(description="视频规格（显示尺寸 + 音轨编码）")
    output_dir: str = Field(description="产物目录")
    font_file: str = Field(description="烧字用的字体文件")
    total_items: int = Field(description="待产出的成片总数")
    completed_items: int = Field(description="已完成的成片数（含失败）")
    failed_items: int = Field(description="失败的成片数")
    skipped_items: int = Field(description="因取消而跳过的成片数")
    current_index: int = Field(description="当前处理的成片序号（0 表示未开始）")
    progress_percent: float = Field(description="当前这条成片的完成百分比（0-100）")
    error_message: str = Field(description="失败原因汇总")
    remark: str = Field(description="备注（用户可编辑，最多 200 字；空串表示没写）")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")
    items: List[FinalcutRenderItemResponse] = Field(
        default_factory=list, description="每条成片的结果；列表接口不返回明细"
    )

    @field_serializer("started_at", "finished_at")
    def _serialize_optional_times(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value) if value is not None else None

    @classmethod
    def from_model(
        cls, model: FinalcutRenderJob, *, include_items: bool = True
    ) -> "FinalcutRenderJobResponse":
        """由 ORM 对象构造响应模型。"""
        return cls(
            id=model.id,
            status=model.status,
            copy_job_id=model.copy_job_id,
            video_path=model.video_path,
            video_duration=model.video_duration,
            video_spec=dict(model.video_spec or {}),
            output_dir=model.output_dir,
            font_file=model.font_file,
            total_items=model.total_items,
            completed_items=model.completed_items,
            failed_items=model.failed_items,
            skipped_items=model.skipped_items,
            current_index=model.current_index,
            progress_percent=model.progress_percent,
            error_message=model.error_message,
            remark=model.remark,
            started_at=model.started_at,
            finished_at=model.finished_at,
            created_at=model.created_at,
            updated_at=model.updated_at,
            items=(
                [FinalcutRenderItemResponse.from_model(item, model.id) for item in model.items]
                if include_items
                else []
            ),
        )


class FinalcutCopyJobListData(BaseModel):
    """文案任务列表数据。"""

    total: int = Field(description="满足条件的总条数")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页条数")
    items: List[FinalcutCopyJobResponse] = Field(description="当前页数据")


class FinalcutRenderJobListData(BaseModel):
    """合成任务列表数据。"""

    total: int = Field(description="满足条件的总条数")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页条数")
    items: List[FinalcutRenderJobResponse] = Field(description="当前页数据")


# --------------------------------------------------------------------------
# 环境与设置
# --------------------------------------------------------------------------


class FinalcutAiStatus(BaseModel):
    """环境自检里的 AI 一项。"""

    configured: bool = Field(description="三要素（base_url/model/key）是否都填了")
    ok: bool = Field(description="这一项是否可用（当前与 configured 同义）")
    base_url: str = Field(description="生效的 API 端点")
    model: str = Field(description="生效的模型名")
    key_present: bool = Field(description="是否已配置 API key")
    key_masked: str = Field(description="API key 掩码（完整值不出后端）")
    detail: str = Field(default="", description="说明")
    fix_hint: str = Field(default="", description="不可用时的修复指引")


class FinalcutFfmpegStatus(BaseModel):
    """环境自检里的 ffmpeg-drawtext 一项。"""

    ok: bool = Field(description="ffmpeg 可用且 drawtext 滤镜在")
    path: str = Field(default="", description="ffmpeg 路径")
    version: str = Field(default="", description="ffmpeg 版本号")
    has_drawtext: bool = Field(description="构建里是否有 drawtext 滤镜")
    supports_boxborderw: bool = Field(description="drawtext 是否支持 boxborderw（5.x 起）")
    detail: str = Field(default="", description="说明")
    fix_hint: str = Field(default="", description="不可用时的修复指引")


class FinalcutFontInfo(BaseModel):
    """探测到的中文字体。"""

    file: str = Field(description="字体文件绝对路径")
    family: str = Field(description="字体名（展示用）")


class FinalcutTextStyleItem(BaseModel):
    """一套上屏样式的展示形态（前端渲染色块用）。"""

    key: str = Field(description="样式标识")
    label: str = Field(description="样式名称")
    preview_text: str = Field(description="预览文字色（CSS）")
    preview_background: str = Field(description="预览底色（CSS）")
    default: bool = Field(description="是否默认样式")


class FinalcutEnvironmentResponse(BaseModel):
    """一键成品的环境自检结果（与 finalcut_env.probe_environment 的键一一对应）。"""

    ready: bool = Field(description="AI、ffmpeg-drawtext、中文字体三项是否都就绪")
    ai: FinalcutAiStatus = Field(description="AI 配置探测")
    ffmpeg: FinalcutFfmpegStatus = Field(description="ffmpeg drawtext 能力探测")
    font: Optional[FinalcutFontInfo] = Field(default=None, description="探测到的中文字体")
    default_output_dir: str = Field(description="默认产物目录（materials/finalcut）")
    text_styles: List[FinalcutTextStyleItem] = Field(description="可切换的上屏样式清单")
    default_style: str = Field(description="默认样式 key")
    copy_count_default: int = Field(description="默认生成的候选文案条数")
    copy_count_max: int = Field(description="一次最多生成的候选文案条数")
    max_items: int = Field(description="单个合成任务最多几条成片")
    chars_per_second: float = Field(
        description="口播语速（字/秒），页面据此算「约念几秒」与字数预算"
    )
    warnings: List[str] = Field(default_factory=list, description="非致命问题提醒")


class AiSettingsResponse(BaseModel):
    """「AI 配置与口播语速」弹窗的读取模型（key 只给掩码；语速一同返回）。"""

    base_url: str = Field(description="生效的 API 端点")
    model: str = Field(description="生效的模型名")
    api_key_present: bool = Field(description="是否已配置 API key")
    api_key_masked: str = Field(description="API key 掩码")
    shadowed_keys: List[str] = Field(default_factory=list, description="被环境变量占据的键")
    warning: str = Field(default="", description="环境变量遮盖 .env 时的提醒")
    chars_per_second: float = Field(description="生效的口播语速（字/秒）")
    chars_per_second_default: float = Field(description="语速的出厂默认值（页面「恢复默认」用）")
    chars_per_second_warning: str = Field(
        default="", description="语速被环境变量遮盖时的提醒"
    )


class AiSettingsUpdate(BaseModel):
    """「AI 配置与口播语速」弹窗的保存请求。

    api_key 留空 = 保持原值不动；chars_per_second 不传 = 不改（两者同一套语义：
    读接口回填不了原值 / 老调用方不带这个字段，都必须能保存成功）。
    """

    base_url: str = Field(..., min_length=1, max_length=500, description="OpenAI 兼容端点")
    model: str = Field(..., min_length=1, max_length=100, description="模型名")
    api_key: str = Field(default="", max_length=200, description="新 API key；留空表示不改")
    chars_per_second: Optional[float] = Field(
        default=None, description="口播语速（字/秒，1.0–15.0）；不传表示不改"
    )


class FinalcutSourceItem(BaseModel):
    """一条历史产物来源（选素材步骤的清单条目）。"""

    path: str = Field(description="产物绝对路径")
    name: str = Field(description="展示名（来源任务 + 文件名）")
    origin: str = Field(description="来源模块：mix（混剪成片）/ subtitle（字幕提取）")
    job_id: int = Field(description="来源任务 id")
    index: int = Field(
        default=0, description="条目在来源任务内的序号（从 1 开始）"
    )
    remark: str = Field(
        default="", description="来源任务的备注（空串表示没写）"
    )
    duration_seconds: Optional[float] = Field(default=None, description="时长（秒）")
    size_bytes: int = Field(default=0, description="文件大小（字节）")
    video_url: str = Field(default="", description="预览播放地址（视频来源才有）")
    thumb_url: str = Field(default="", description="封面地址（视频来源才有）")


class FinalcutSourcesResponse(BaseModel):
    """历史产物来源清单。"""

    subtitles: List[FinalcutSourceItem] = Field(description="字幕提取任务的 .srt 产物")
    videos: List[FinalcutSourceItem] = Field(description="混剪任务的成片产物")
