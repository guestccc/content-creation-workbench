"""FastAPI 应用入口。

启动方式：
    uv run uvicorn app.main:app --reload
或：
    uv run python -m app.main
"""

import atexit
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.materials import ensure_materials_layout
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.db.init_db import init_db
from app.db.session import engine
from app.services.crawl_job_worker import crawl_job_worker
from app.services.crawl_runner import recover_interrupted_jobs as recover_interrupted_crawl_jobs
from app.services.mix_job_worker import mix_job_worker
from app.services.mix_runner import recover_interrupted_jobs as recover_interrupted_mix_jobs
from app.services.scene_job_worker import scene_job_worker
from app.services.scene_runner import recover_interrupted_jobs
from app.services.subtitle_job_worker import subtitle_job_worker
from app.services.subtitle_runner import recover_interrupted_jobs as recover_interrupted_subtitle_jobs

# 先初始化日志，保证后续所有模块的日志都能正常输出
setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。

    镜头分割工作线程只能在这里 start —— uvicorn --reload 的 reloader 父进程
    会 import 本模块（setup_logging/create_app 都会在父进程跑一遍），但父进程
    永不调用 lifespan。若把 worker.start() 写在模块级或 create_app() 里，
    父子两个进程会各起一个 worker 抢同一个 SQLite、重复执行任务。
    """
    logger.info("正在启动 %s v%s", settings.APP_NAME, settings.APP_VERSION)
    try:
        init_db()
    except Exception:
        # 数据库初始化失败不应阻止服务启动，健康检查接口会暴露该问题
        logger.exception("数据库初始化失败，相关接口将不可用")

    # materials/ 整个被 .gitignore 挡在 git 外面，新克隆的仓库上它并不存在。
    # 启动时按规划把骨架建出来（source / clips / subtitle / output），
    # 镜头分割页一打开就是规划好的样子（建不出来会退回主目录并记日志）。
    logger.info("素材目录：%s", ensure_materials_layout())

    if settings.SCENE_WORKER_ENABLED:
        # 回收上次异常退出留下的 running 任务（含残留子进程），pending 不动
        recover_interrupted_jobs()
        # 双 Ctrl-C 这类硬退出走不到下面的 shutdown 流程，atexit 兜底停 worker
        atexit.register(_shutdown_scene_worker)
        scene_job_worker.start()

    if settings.MIX_WORKER_ENABLED:
        # 混剪：同一套 DB 即队列模式，回收与启停逻辑与镜头分割完全同构
        recover_interrupted_mix_jobs()
        atexit.register(_shutdown_mix_worker)
        mix_job_worker.start()

    if settings.SUBTITLE_WORKER_ENABLED:
        # 字幕提取：同一套 DB 即队列模式，回收与启停逻辑与镜头分割完全同构
        recover_interrupted_subtitle_jobs()
        atexit.register(_shutdown_subtitle_worker)
        subtitle_job_worker.start()

    if settings.CRAWL_WORKER_ENABLED:
        # 素材抓取：同一套 DB 即队列模式，回收与启停逻辑与镜头分割完全同构
        recover_interrupted_crawl_jobs()
        atexit.register(_shutdown_crawl_worker)
        crawl_job_worker.start()

    yield

    logger.info("正在关闭应用")
    if settings.SCENE_WORKER_ENABLED:
        atexit.unregister(_shutdown_scene_worker)
        _shutdown_scene_worker()
    if settings.MIX_WORKER_ENABLED:
        atexit.unregister(_shutdown_mix_worker)
        _shutdown_mix_worker()
    if settings.SUBTITLE_WORKER_ENABLED:
        atexit.unregister(_shutdown_subtitle_worker)
        _shutdown_subtitle_worker()
    if settings.CRAWL_WORKER_ENABLED:
        atexit.unregister(_shutdown_crawl_worker)
        _shutdown_crawl_worker()
    logger.info("释放数据库连接池")
    engine.dispose()


def _shutdown_scene_worker() -> None:
    """停止镜头分割工作线程（有界等待，绝不阻塞热重载）。"""
    try:
        scene_job_worker.stop()
    except Exception:  # noqa: BLE001 - 关闭路径兜底，绝不能挂住进程退出
        logger.exception("停止镜头分割工作线程出现异常")


def _shutdown_mix_worker() -> None:
    """停止混剪工作线程（有界等待，绝不阻塞热重载）。"""
    try:
        mix_job_worker.stop()
    except Exception:  # noqa: BLE001 - 关闭路径兜底，绝不能挂住进程退出
        logger.exception("停止混剪工作线程出现异常")


def _shutdown_subtitle_worker() -> None:
    """停止字幕提取工作线程（有界等待，绝不阻塞热重载）。"""
    try:
        subtitle_job_worker.stop()
    except Exception:  # noqa: BLE001 - 关闭路径兜底，绝不能挂住进程退出
        logger.exception("停止字幕提取工作线程出现异常")


def _shutdown_crawl_worker() -> None:
    """停止素材抓取工作线程（有界等待，绝不阻塞热重载）。"""
    try:
        crawl_job_worker.stop()
    except Exception:  # noqa: BLE001 - 关闭路径兜底，绝不能挂住进程退出
        logger.exception("停止素材抓取工作线程出现异常")


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
