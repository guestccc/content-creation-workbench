"""智能配音（Voicebox）相关的 Pydantic 模型。

包含五组：
1. 环境自检 —— 本机 Voicebox 服务在不在、模型下没下、有没有 GPU；
2. 模型 —— 能用来配音的模型清单（只读，下载/删除在 Voicebox 里做）；
3. 音色 —— 音色列表（只读，建音色在 Voicebox 自己的界面里做）；
4. 生成 —— 提交一次配音、轮询它的进度；
5. 产物 —— materials/dubbing/ 下的音频清单。

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

#: 视频配音的语言白名单。**上游支持的比这多得多**（它的正则收了二十几种），
#: 这里只放开中英文：配这个工具的产出是中文短视频，别的语言没人用，而多语言
#: 能力还取决于所选 engine（Kokoro 就是英文为主）。要放开时两边一起改。
LANGUAGE_OPTIONS = ("zh", "en")

# 能选的模型不在本文件写死 —— 它由 services/voicebox_models.py 的 CATALOG 定义，
# 并经 GET /voicebox/models 下发（那张表还要跟上游 /models/status 对上下载状态，
# 不是一份单纯的白名单）。生成接口的参数校验也是查那张表，见 api/v1/voicebox.py。


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

    # 下面八个字段只为回答一件事：页面上那两个按钮（「设置镜像」「重启 Voicebox」）
    # 该不该出现、该说什么话。**前端不做平台判断** —— 所有差异都在后端算好，
    # 页面只按这些布尔值与文案渲染（同 subtitle_env 的 install_hints 约定）。
    platform: str = Field(default="", description="后端所在系统：macos / windows / linux")
    platform_label: str = Field(default="", description="系统名的中文展示")
    hf_mirror_supported: bool = Field(
        default=False,
        description="能否由本工具设置模型下载源（系统支持且 Voicebox 就在本机）",
    )
    hf_mirror_value: str = Field(
        default="", description="当前 HF_ENDPOINT 的值；空串表示未设置"
    )
    hf_mirror_is_recommended: bool = Field(
        default=False, description="当前下载源是不是本页推荐的镜像"
    )
    hf_mirror_persistent: bool = Field(
        default=False, description="下载源在注销 / 重启后是否仍然有效"
    )
    voicebox_app_path: str = Field(
        default="", description="找到的 Voicebox 安装位置；空串表示没找到"
    )
    restart_supported: bool = Field(
        default=False, description="页面是否该显示「重启 Voicebox」"
    )


class VoiceboxRestartResponse(BaseModel):
    """一次「重启 Voicebox」的结果。

    **不代表服务已就绪**：桌面端冷启动到 /health 可用约 30 秒，而前端请求 15 秒
    就超时了，所以这个接口不等就绪、立即返回；就绪与否由前端轮询 GET /environment
    判断（与切割 / 字幕 / 生成同一套「任务异步跑、进度靠轮询」）。
    """

    started: bool = Field(description="是否已发起启动（不代表服务已经就绪）")
    app_path: str = Field(default="", description="拉起的安装位置；空串表示按名字查找拉起")
    detail: str = Field(default="", description="给用户看的一句话")
    wait_hint: str = Field(default="", description="要等多久 / 等的是什么")


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


class DubbingModelItem(BaseModel):
    """一个能用来配音的模型（= 上游 engine + model_size 的一个组合）。"""

    engine: str = Field(description="上游 /generate 的 engine 参数")
    model_size: str = Field(
        description="上游 /generate 的 model_size 参数；**空串表示不分尺寸**，"
        "提交时这个字段不发"
    )
    model_name: str = Field(description="上游 /models/status 里的标识")
    label: str = Field(description="页面上显示的名字")
    note: str = Field(default="", description="一句话说明它适合什么场景")
    downloaded: Optional[bool] = Field(
        default=None, description="是否已下载；null 表示上游列表里没这个模型（可能是版本差异）"
    )
    downloading: bool = Field(default=False, description="是否正在下载")
    loaded: bool = Field(default=False, description="是否已加载进显存/内存")
    size_mb: Optional[float] = Field(default=None, description="已下载时占用的体积（MB）")


class DubbingModelListData(BaseModel):
    """配音可选模型清单（顺序即页面下拉顺序）。"""

    items: List[DubbingModelItem] = Field(description="模型清单")
    total: int = Field(description="模型数量")


class DubbingGenerationCreate(BaseModel):
    """提交一次配音生成。

    `engine` 与 `model_size` 一起指向一个具体模型（见 DubbingModelItem）：
    后端按目录校验这个组合存不存在，而不是各自查一份白名单。
    """

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
    engine: str = Field(default="qwen", description="TTS 引擎（来自模型列表）")
    model_size: str = Field(
        default="1.7B",
        description="模型规模（来自模型列表）；部分引擎不分尺寸，此时传空串",
    )


class DubbingGenerationResponse(BaseModel):
    """一次配音生成的进度记录（只活在内存里，重启即空）。"""

    id: int = Field(description="进程内自增 id，轮询用它")
    status: str = Field(description="queued / running / success / failed")
    text_excerpt: str = Field(description="文案摘要（前若干字，列表里展示用）")
    profile_id: str = Field(description="音色 id")
    profile_name: str = Field(default="", description="音色名")
    engine: str = Field(default="qwen", description="这次用的 TTS 引擎")
    model_size: str = Field(default="", description="这次用的模型规模；空串表示不分尺寸")
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
