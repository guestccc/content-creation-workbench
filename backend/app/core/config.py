"""应用配置模块。

配置项通过环境变量或 .env 文件注入，避免硬编码敏感信息。
优先级：环境变量 > .env 文件 > 代码内默认值。
"""

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def repo_root() -> Path:
    """本仓库（内容创作工作台）的根目录。

    本文件位于 <仓库>/backend/app/core/config.py，所以 parents[3] 是仓库根。
    它是「仓库内的绝对路径」类默认值的唯一来源（vct、materials/ 等）。
    """
    return Path(__file__).resolve().parents[3]


def toolbox_root() -> Optional[Path]:
    """工具箱根目录（仓库的上一层），VideoCaptioner 等平级工具的存放处。

    把仓库单独拷到别处时这一层可能不存在（下标越界），此时返回 None ——
    配置的默认值不该让整个应用起不来，探测不到会在页面上如实报告。
    """
    try:
        return Path(__file__).resolve().parents[4]
    except IndexError:
        return None


def default_vc_root() -> str:
    """VideoCaptioner 的默认安装位置：工具箱根目录下的 VideoCaptioner/。

    注意这只是**一个**默认候选，不是唯一答案：真实目录名常带下载解压留下的
    后缀（VideoCaptioner-master 之类），所以 services/subtitle_env.py 还会
    扫同级目录做模糊匹配，页面上也能手动指定。见该模块的 _discover_roots()。

    公开而非私有，是因为 services/subtitle_settings.py 在「用户把 .env 里
    那行整个删掉」时要拿它当回退值 —— 默认值必须只有这一个来源。
    """
    base = toolbox_root()
    if base is None:
        return str(repo_root() / "VideoCaptioner")
    return str(base / "VideoCaptioner")


def default_mc_root() -> str:
    """MediaCrawler 的默认位置：工具箱根目录下的 MediaCrawler/。

    与 default_vc_root 同一套理由：仓库被单独拷走时 toolbox_root() 可能
    推不出来，退回仓库内路径 —— 探测不到会在环境自检里如实报告并给指引。
    """
    base = toolbox_root()
    if base is None:
        return str(repo_root() / "MediaCrawler")
    return str(base / "MediaCrawler")


class Settings(BaseSettings):
    """全局配置对象。

    字段名与同名环境变量一一对应（大小写不敏感）。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 应用信息 ----------
    APP_NAME: str = "内容创作工作台"
    APP_VERSION: str = "0.1.0"
    APP_DESCRIPTION: str = "面向内容创作者的选题、创作与发布管理工作台"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # ---------- 服务监听 ----------
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # ---------- 数据库 ----------
    # 默认使用 SQLite；生产环境可替换为 PostgreSQL 等，仅需修改此连接串
    DATABASE_URL: str = "sqlite:///./workbench.db"
    DB_ECHO: bool = False

    # ---------- 跨域白名单 ----------
    CORS_ORIGINS: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )

    # ---------- 日志 ----------
    LOG_LEVEL: str = "INFO"

    # ---------- 智能镜头分割 ----------
    # vct 命令行工具的路径。vct + vctl 已作为仓库一部分维护在
    # 内容创作工作台/vct 与 内容创作工作台/vctl/。
    SCENE_VCT_PATH: str = str(repo_root() / "vct")
    # 素材目录的**根**，默认是仓库根目录的 materials/。
    # 整个目录被 .gitignore 挡在 git 外面（原片与产物都太大），
    # 由后端启动时按 app/core/materials.py 里的规划建好 source / clips /
    # subtitle / output 四个分段，新克隆的仓库因此也是规划好的样子。
    SCENE_MATERIALS_DIR: str = str(repo_root() / "materials")
    # 是否启用后台工作线程。测试环境置 False，避免 worker 与测试会话抢连接。
    SCENE_WORKER_ENABLED: bool = True
    # 工作线程空闲时的轮询间隔（秒）。
    SCENE_JOB_POLL_SECONDS: float = 1.0
    # 执行单个视频时检查进度/取消信号的间隔（秒）。
    SCENE_JOB_TICK_SECONDS: float = 0.5
    # 进度字段落库的最小间隔（秒），避免高频写库。
    SCENE_JOB_PROGRESS_SECONDS: float = 3.0
    # 单个视频的硬超时（秒）。超时整组 kill，防止一条卡住的 vct 堵死整个队列。
    SCENE_JOB_VIDEO_TIMEOUT_SECONDS: int = 3600
    # 停止工作线程 / 取消任务时，等待子进程组自行退出的宽限（秒），之后强杀。
    SCENE_JOB_STOP_GRACE_SECONDS: float = 3.0
    # 可处理的视频扩展名（小写、带点）。前端据此标记可选文件，后端据此枚举目录。
    SCENE_INPUT_EXTENSIONS: List[str] = Field(
        default_factory=lambda: [".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"]
    )
    # 单个任务最多处理的视频数，防止误选一个几千条素材的目录把队列占满。
    SCENE_MAX_BATCH_FILES: int = 200
    # 列目录接口单次返回的最大条目数，超出截断并置 truncated 标记。
    SCENE_FS_LIST_LIMIT: int = 500

    # ---------- 智能混剪 ----------
    # 素材目录由用户在页面上自己添加（不限定在 materials/ 里），
    # 但混剪的成片默认落在镜头分割那一套的 materials/output/，不重复定义目录配置。
    # 最多能添加几个素材目录（注册表落在素材根的 .mix-sources.json）。
    MIX_MAX_SOURCES: int = 20
    # 每个素材目录向下扫描几层子目录。必须有上限：用户可能把主目录整个加进来。
    MIX_SOURCE_SCAN_DEPTH: int = 4
    # 单个素材目录最多收录多少条视频，防止误加一个几万条素材的盘把页面拖垮。
    MIX_MAX_CLIPS_PER_SOURCE: int = 2000
    # 是否启用混剪后台工作线程。测试环境置 False，与 SCENE_WORKER_ENABLED 同一套理由。
    MIX_WORKER_ENABLED: bool = True
    # 工作线程空闲时的轮询间隔（秒）。
    MIX_JOB_POLL_SECONDS: float = 1.0
    # 执行单个 ffmpeg 子进程时检查进度/取消信号的间隔（秒）。
    MIX_JOB_TICK_SECONDS: float = 0.5
    # 进度字段落库的最小间隔（秒），避免高频写库。
    MIX_JOB_PROGRESS_SECONDS: float = 3.0
    # 单个任务的整体硬超时（秒）。
    MIX_JOB_TIMEOUT_SECONDS: int = 7200
    # 单个片段归一化的硬超时（秒）。
    MIX_JOB_ITEM_TIMEOUT_SECONDS: int = 600
    # 停止工作线程 / 取消任务时，等待子进程组自行退出的宽限（秒），之后强杀。
    MIX_JOB_STOP_GRACE_SECONDS: float = 3.0
    # 单任务最多产出几条成片。
    MIX_MAX_OUTPUTS: int = 20
    # 每个列表（开头/中间/结尾）最多选多少条，防止误选把队列占满。
    MIX_MAX_CLIPS_PER_LIST: int = 100
    # 归一化编码质量：CRF 越小画质越好体积越大，18 是「视觉无损」的常用档。
    MIX_ENCODE_CRF: int = 18
    # 归一化编码速度档：veryfast 在画质几乎不变的前提下明显快于 medium。
    MIX_ENCODE_PRESET: str = "veryfast"

    # ---------- 视频字幕提取 ----------
    # VideoCaptioner 的安装根目录。它**不在本仓库里**，而是与本仓库平级：
    # 本文件位于 <工具箱>/内容创作工作台/backend/app/core/config.py，
    # parents[3] = 内容创作工作台，parents[4] = 内容制作工具（工具箱根），
    # VideoCaptioner 就放在工具箱根下。注意别照抄 vctl/env.py 里的
    # VC_ROOT —— 那边指的是仓库内的路径，本机并不存在。
    SUBTITLE_VC_ROOT: str = default_vc_root()
    # 显式指定要调用的解释器/可执行文件，留空表示自动探测（见 services/subtitle_env.py）。
    # 给「自动探测找不到、但用户知道自己装在哪儿」的情况留的后门。
    SUBTITLE_VC_PYTHON: str = ""
    # 是否启用后台工作线程。测试环境置 False，与 SCENE_WORKER_ENABLED 同一套理由。
    SUBTITLE_WORKER_ENABLED: bool = True
    # 工作线程空闲时的轮询间隔（秒）。
    SUBTITLE_JOB_POLL_SECONDS: float = 1.0
    # 执行单个视频时检查取消信号的间隔（秒）。
    SUBTITLE_JOB_TICK_SECONDS: float = 0.5
    # 进度字段落库的最小间隔（秒），避免高频写库。
    SUBTITLE_JOB_PROGRESS_SECONDS: float = 3.0
    # 单条视频的硬超时（秒）。转写比切割慢得多，给得比 SCENE 宽一倍。
    SUBTITLE_JOB_VIDEO_TIMEOUT_SECONDS: int = 1800
    # 停止工作线程 / 取消任务时，等待子进程组自行退出的宽限（秒），之后强杀。
    SUBTITLE_JOB_STOP_GRACE_SECONDS: float = 3.0
    # 可处理的视频扩展名（小写、带点）。
    SUBTITLE_INPUT_EXTENSIONS: List[str] = Field(
        default_factory=lambda: [".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"]
    )
    # 单个任务最多处理的视频数，防止误选一个几千条素材的目录把队列占满。
    SUBTITLE_MAX_BATCH_FILES: int = 200
    # 默认 ASR 引擎，取值见 app/core/subtitle_asr.py。bijian 免费、免配置、中英通吃。
    SUBTITLE_DEFAULT_ASR: str = "bijian"
    # VideoCaptioner 探测结果的缓存秒数。探测要起一次子进程（约 0.1 秒），
    # 不能每次请求都做；前端「重新检测」按钮走 force=True 绕过缓存。
    SUBTITLE_DETECT_CACHE_SECONDS: float = 30.0
    # 字幕预览接口最多返回多少字节（超出只截取开头并置 truncated）。
    SUBTITLE_PREVIEW_MAX_BYTES: int = 512 * 1024

    # ---------- 素材抓取（MediaCrawler） ----------
    # MediaCrawler 的仓库根目录。它与本仓库平级（工具箱根下），自带 .venv
    # 与登录态缓存，抓取任务全部以「子进程调它的 main.py」方式执行 ——
    # 它的配置是全局模块变量，import 进本进程会被多任务互相污染，CLI 是
    # 它官方 WebUI 也在用的隔离边界。
    CRAWL_MC_ROOT: str = default_mc_root()
    # 是否启用后台工作线程。测试环境置 False，与 SCENE_WORKER_ENABLED 同一套理由。
    CRAWL_WORKER_ENABLED: bool = True
    # 工作线程空闲时的轮询间隔（秒）。
    CRAWL_JOB_POLL_SECONDS: float = 1.0
    # 盯 MC 子进程时检查取消/超时信号的间隔（秒）。
    CRAWL_JOB_TICK_SECONDS: float = 0.5
    # 进度字段落库的最小间隔（秒），避免高频写库。
    CRAWL_JOB_PROGRESS_SECONDS: float = 3.0
    # 单个任务的整体硬超时（秒）。抓取 200 条加下载媒体最多十几分钟，1 小时足够宽。
    CRAWL_JOB_TIMEOUT_SECONDS: int = 3600
    # 停止工作线程 / 取消任务时，等待 MC 子进程组自行退出的宽限（秒）。
    # 比字幕略长：MC 收尾要清理它拉起的 CDP 浏览器，强杀太早会留孤儿 Chrome。
    CRAWL_JOB_STOP_GRACE_SECONDS: float = 5.0
    # 单任务最多抓多少条（反爬自保：一口气几千条必然触发风控）。
    CRAWL_MAX_NOTES_LIMIT: int = 200
    # 并发抓取数的上限（MC 官方默认 1，加大容易触发风控）。
    CRAWL_MAX_CONCURRENCY_LIMIT: int = 3
    # MC 环境探测结果的缓存秒数（探测要起子进程 + 扫目录，不能每请求都做）。
    CRAWL_ENV_CACHE_SECONDS: float = 30.0

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors_origins(cls, value):
        """支持用逗号分隔的字符串配置跨域白名单。

        例如：CORS_ORIGINS=http://a.com,http://b.com
        同时兼容标准 JSON 数组写法。
        """
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("SCENE_INPUT_EXTENSIONS", "SUBTITLE_INPUT_EXTENSIONS", mode="before")
    @classmethod
    def _split_scene_extensions(cls, value):
        """支持用逗号分隔的字符串配置视频扩展名。

        例如：SCENE_INPUT_EXTENSIONS=.mp4,.mov
        同时兼容标准 JSON 数组写法；统一转小写并补上点号前缀。
        镜头分割与字幕提取共用这一份规范化逻辑（扩展名的写法没有理由不同）。
        """
        if isinstance(value, str) and not value.strip().startswith("["):
            value = [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [
                ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in value
            ]
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局唯一配置实例（带缓存，避免重复解析 .env 文件）。"""
    return Settings()


# 素材目录的路径解析与目录规划在 app/core/materials.py，本模块只负责配置项本身。
settings: Settings = get_settings()
