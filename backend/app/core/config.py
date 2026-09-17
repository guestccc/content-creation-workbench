"""应用配置模块。

配置项通过环境变量或 .env 文件注入，避免硬编码敏感信息。
优先级：环境变量 > .env 文件 > 代码内默认值。
"""

from functools import lru_cache
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局唯一配置实例（带缓存，避免重复解析 .env 文件）。"""
    return Settings()


settings: Settings = get_settings()
