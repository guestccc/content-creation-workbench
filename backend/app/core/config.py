"""应用配置模块。

配置项通过环境变量或 .env 文件注入，避免硬编码敏感信息。
优先级：环境变量 > .env 文件 > 代码内默认值。
"""

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # 内容创作工作台/vct 与 内容创作工作台/vctl/：
    # 本文件位于 backend/app/core/，parents[3] 就是 内容创作工作台。
    SCENE_VCT_PATH: str = str(Path(__file__).resolve().parents[3] / "vct")
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

    @field_validator("SCENE_INPUT_EXTENSIONS", mode="before")
    @classmethod
    def _split_scene_extensions(cls, value):
        """支持用逗号分隔的字符串配置视频扩展名。

        例如：SCENE_INPUT_EXTENSIONS=.mp4,.mov
        同时兼容标准 JSON 数组写法；统一转小写并补上点号前缀。
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


settings: Settings = get_settings()
