"""FastAPI 应用入口。

启动方式：
    uv run uvicorn app.main:app --reload
或：
    uv run python -m app.main
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.db.init_db import init_db
from app.db.session import engine

# 先初始化日志，保证后续所有模块的日志都能正常输出
setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动时建表，关闭时释放连接池。"""
    logger.info("正在启动 %s v%s", settings.APP_NAME, settings.APP_VERSION)
    try:
        init_db()
    except Exception:
        # 数据库初始化失败不应阻止服务启动，健康检查接口会暴露该问题
        logger.exception("数据库初始化失败，相关接口将不可用")

    yield

    logger.info("正在关闭应用，释放数据库连接池")
    engine.dispose()


def create_app() -> FastAPI:
    """应用工厂：装配中间件、异常处理器与路由。

    Returns:
        配置完成的 FastAPI 实例。
    """
    application = FastAPI(
        title=settings.APP_NAME,
        description=settings.APP_DESCRIPTION,
        version=settings.APP_VERSION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # 跨域配置：仅放行白名单内的前端地址
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 统一异常处理，必须在路由注册前完成
    register_exception_handlers(application)

    application.include_router(api_router, prefix=settings.API_V1_PREFIX)

    @application.get("/", tags=["系统"], summary="服务根路径")
    def root() -> dict:
        """返回服务基本信息，便于快速确认服务是否启动。"""
        return {
            "success": True,
            "data": {
                "name": settings.APP_NAME,
                "version": settings.APP_VERSION,
                "docs": "/docs",
            },
        }

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
    )
