"""智能配音（Voicebox）相关的 Pydantic 模型。

包含四组：
1. 环境自检 —— 本机 Voicebox 服务在不在、模型下没下、有没有 GPU；
2. 音色 —— 音色列表（只读，建音色在 Voicebox 自己的界面里做）；
3. 生成 —— 提交一次配音、轮询它的进度；
4. 产物 —— materials/dubbing/ 下的音频清单。

与其它功能的一处结构差异：**没有任务表**。Voicebox 是常驻外部服务，我们只做
HTTP 代理，所以生成记录只活在进程内存里（见 services/voicebox_generation.py），
ID 是进程内自增的 int。用 int 而不是 uuid 是有意的：前端
`useJobPolling` / `useJobRunner` 的泛型约束是 `J extends { id: number }`，
int 让它们能直接复用，不必为这个页面另写一套轮询。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer

from app.core.config import settings
from app.schemas.common import to_utc_iso

#: 视频配音的语言白名单，与上游 GenerationRequest 的 `^(en|zh)$` 对齐。
LANGUAGE_OPTIONS = ("zh", "en")

#: 模型规模白名单，与上游的 `^(1\.7B|0\.6B)$` 对齐。
MODEL_SIZE_OPTIONS = ("1.7B", "0.6B")


class VoiceboxInstallHint(BaseModel):
    """一条安装/启动指引。命令只是文本，供前端展示与复制。

    与 schemas/subtitle_job.py、schemas/crawl_job.py 里的同名模型形状一致
    （两处各自独立声明，是既有约定；合成一份公共模型不在本次范围内）。
    """

    title: str = Field(description="这一步做什么")
    command: str = Field(default="", description="要执行的命令；为空表示这一步没有命令")
    note: str = Field(default="", description="补充说明")
    url: str = Field(default="", description="相关链接")


class VoiceboxEnvironmentResponse(BaseModel):
    """智能配音功能的运行环境自检结果。"""

    ready: bool = Field(description="能否开始生成：服务连得上且模型不是「未下载」")
    base_url: str = Field(description="后端实际调用的 Voicebox 服务地址")
    base_url_source: str = Field(
        description="该地址来自哪一层：environment / env_file / default"
    )
    reachable: bool = Field(description="服务是否连得上（连不上不是错误，是正常状态）")
    status: str = Field(default="", description="上游 /health 报告的状态字符串")
    model_loaded: bool = Field(default=False, description="模型当前是否已加载进显存/内存")
    model_downloaded: Optional[bool] = Field(
        default=None, description="模型是否已下载；上游拿不到时为 null"
    )
    model_size: Optional[str] = Field(default=None, description="当前生效的模型规模")
    gpu_available: bool = Field(default=False, description="是否有可用的 GPU 加速")
    vram_used_mb: Optional[float] = Field(default=None, description="已占用的显存（MB）")
    profile_count: int = Field(default=0, description="Voicebox 里的音色数量")
    default_output_dir: str = Field(description="配音产物默认落盘目录（materials/dubbing）")
    detail: str = Field(default="", description="一句话说明当前状态或失败原因")
    fix_hint: str = Field(default="", description="未就绪时的下一步动作，直接展示给用户")
    install_hints: List[VoiceboxInstallHint] = Field(
        default_factory=list, description="连不上时的分步指引（纯文本，后端不代装）"
    )
    warnings: List[str] = Field(default_factory=list, description="需要提醒用户的情况")


class VoiceboxBaseUrlUpdate(BaseModel):
    """指定 Voicebox 服务地址（写进 backend/.env）。"""

    base_url: str = Field(
        ...,
        max_length=500,
        description="服务地址，如 http://127.0.0.1:17493；缺 scheme 时按 http 补全",
    )


class VoiceProfileItem(BaseModel):
    """一个音色（Voicebox 侧的音色档案）。"""

    id: str = Field(description="音色 id（提交生成时用它）")
    name: str = Field(description="音色名")
    description: Optional[str] = Field(default=None, description="音色说明，可空")
    language: str = Field(default="", description="音色标注的语言")


class VoiceProfileListData(BaseModel):
    """音色列表。"""

    items: List[VoiceProfileItem] = Field(description="音色清单")
    total: int = Field(description="音色数量")


class DubbingGenerationCreate(BaseModel):
    """提交一次配音生成。"""

    text: str = Field(
        ...,
        min_length=1,
        max_length=settings.VOICEBOX_MAX_TEXT_CHARS,
        description=f"要配音的文案，最多 {settings.VOICEBOX_MAX_TEXT_CHARS} 字",
    )
    profile_id: str = Field(..., min_length=1, description="音色 id（来自音色列表）")
    profile_name: str = Field(
        default="", description="音色名（只用于记录与展示，不参与生成）"
    )
    filename: str = Field(
        default="",
        max_length=120,
        description="产物文件名（不含扩展名）；留空按 dub_<时间戳> 自动命名",
    )
    language: str = Field(default="zh", description="语言：zh / en")
    model_size: str = Field(default="1.7B", description="模型规模：1.7B / 0.6B")


class DubbingGenerationResponse(BaseModel):
    """一次配音生成的进度记录（只活在内存里，重启即空）。"""

    id: int = Field(description="进程内自增 id，轮询用它")
    status: str = Field(description="queued / running / success / failed")
    text_excerpt: str = Field(description="文案摘要（前若干字，列表里展示用）")
    profile_id: str = Field(description="音色 id")
    profile_name: str = Field(default="", description="音色名")
    filename: str = Field(default="", description="产物文件名（含扩展名），成功后有值")
    output_path: str = Field(default="", description="产物绝对路径，成功后有值")
    duration: Optional[float] = Field(default=None, description="音频时长（秒）")
    error_message: str = Field(default="", description="失败原因（用户可读）")
    progress_hint: str = Field(default="", description="当前阶段的一句话说明")
    created_at: Optional[datetime] = Field(default=None, description="提交时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")
    elapsed_seconds: float = Field(default=0.0, description="已耗时（秒）")

    @field_serializer("created_at", "finished_at")
    def _serialize_optional_times(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value) if value is not None else None


class DubbingAudioItem(BaseModel):
    """materials/dubbing/ 下的一份配音产物。"""

    name: str = Field(description="文件名（含扩展名）")
    audio_url: str = Field(description="音频流地址（后端给，前端不拼）")
    size_bytes: int = Field(description="文件大小（字节）")
    duration: Optional[float] = Field(
        default=None, description="音频时长（秒）；索引里没有则为 null（不现场探测）"
    )
    profile_name: str = Field(default="", description="生成时用的音色名")
    text_excerpt: str = Field(default="", description="生成时用的文案摘要")
    created_at: Optional[datetime] = Field(default=None, description="生成时间")
    indexed: bool = Field(
        description="是否在索引里：false 表示这个文件不是本功能生成的（用户自己放进来的）"
    )

    @field_serializer("created_at")
    def _serialize_created_at(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value) if value is not None else None


class DubbingAudioListData(BaseModel):
    """配音产物清单。"""

    items: List[DubbingAudioItem] = Field(description="产物清单（新的在前）")
    total: int = Field(description="总数")
    dir: str = Field(description="产物目录绝对路径")
