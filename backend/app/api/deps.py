"""FastAPI 依赖注入定义。

集中管理依赖类型别名，路由函数直接使用 Annotated 类型即可，
避免每个接口重复写 Depends(...)。
"""

from typing import Annotated, Callable

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import SessionLocal, get_db
from app.services.account_service import AccountService
from app.services.background_job_service import BackgroundJobService
from app.services.creator_service import CreatorService
from app.services.crawl_cookie_service import CrawlCookieService
from app.services.content_service import ContentService
from app.services.crawl_job_service import CrawlJobService
from app.services.crawl_note_copy_service import CrawlNoteCopyService
from app.services.finalcut_copy_service import FinalcutCopyJobService
from app.services.finalcut_render_service import FinalcutRenderJobService
from app.services.mix_job_service import MixJobService
from app.services.publish_task_service import PublishTaskService
from app.services.scene_job_service import SceneJobService
from app.services.subtitle_job_service import SubtitleJobService

# 数据库会话依赖
DbSession = Annotated[Session, Depends(get_db)]


def get_session_factory() -> Callable[[], Session]:
    """给「响应返回之后才跑」的流式生成器用的会话工厂。

    为什么不用 DbSession：流式端点的生成器由 Starlette 在响应阶段才迭代，
    那时请求作用域的会话可能已经被依赖清理关掉了（见 crawl_note_copy_service
    的模块 docstring）。所以给的是工厂而不是会话实例 —— 生成器自己开、自己关。

    单独抽成一个依赖（而不是在 service 里直接 import SessionLocal），是为了让
    测试能整体覆盖到内存库：直接引用会绕过 conftest 对 get_db 的覆盖，把测试
    数据写进真实 workbench.db。
    """
    return SessionLocal


def get_content_service(db: DbSession) -> ContentService:
    """构造内容服务实例。

    Args:
        db: 由 FastAPI 注入的数据库会话。

    Returns:
        绑定当前请求会话的 ContentService。
    """
    return ContentService(db)


def get_account_service(db: DbSession) -> AccountService:
    """构造账号服务实例。

    Args:
        db: 由 FastAPI 注入的数据库会话。

    Returns:
        绑定当前请求会话的 AccountService。
    """
    return AccountService(db)


def get_publish_task_service(db: DbSession) -> PublishTaskService:
    """构造发布任务服务实例。

    Args:
        db: 由 FastAPI 注入的数据库会话。

    Returns:
        绑定当前请求会话的 PublishTaskService。
    """
    return PublishTaskService(db)


def get_scene_job_service(db: DbSession) -> SceneJobService:
    """构造镜头分割任务服务实例。

    Args:
        db: 由 FastAPI 注入的数据库会话。

    Returns:
        绑定当前请求会话的 SceneJobService。
    """
    return SceneJobService(db)


def get_mix_job_service(db: DbSession) -> MixJobService:
    """构造混剪任务服务实例。"""
    return MixJobService(db)


def get_subtitle_job_service(db: DbSession) -> SubtitleJobService:
    """构造字幕提取任务服务实例。"""
    return SubtitleJobService(db)


def get_background_job_service(db: DbSession) -> BackgroundJobService:
    """构造一键换背景任务服务实例。"""
    return BackgroundJobService(db)


def get_crawl_job_service(db: DbSession) -> CrawlJobService:
    """构造素材抓取任务服务实例。"""
    return CrawlJobService(db)


def get_crawl_note_copy_service(db: DbSession) -> CrawlNoteCopyService:
    """构造素材抓取笔记 AI 文案服务实例。"""
    return CrawlNoteCopyService(db)


def get_creator_service(db: DbSession) -> CreatorService:
    """构造创作者主页服务实例。"""
    return CreatorService(db)


def get_crawl_cookie_service(db: DbSession) -> CrawlCookieService:
    """构造 Cookie 库服务实例。"""
    return CrawlCookieService(db)


def get_finalcut_copy_job_service(db: DbSession) -> FinalcutCopyJobService:
    """构造一键成品·文案任务服务实例。"""
    return FinalcutCopyJobService(db)


def get_finalcut_render_job_service(db: DbSession) -> FinalcutRenderJobService:
    """构造一键成品·合成任务服务实例。"""
    return FinalcutRenderJobService(db)


# 服务依赖类型别名
SessionFactoryDep = Annotated[Callable[[], Session], Depends(get_session_factory)]
ContentServiceDep = Annotated[ContentService, Depends(get_content_service)]
AccountServiceDep = Annotated[AccountService, Depends(get_account_service)]
PublishTaskServiceDep = Annotated[PublishTaskService, Depends(get_publish_task_service)]
SceneJobServiceDep = Annotated[SceneJobService, Depends(get_scene_job_service)]
MixJobServiceDep = Annotated[MixJobService, Depends(get_mix_job_service)]
SubtitleJobServiceDep = Annotated[SubtitleJobService, Depends(get_subtitle_job_service)]
BackgroundJobServiceDep = Annotated[BackgroundJobService, Depends(get_background_job_service)]
CrawlJobServiceDep = Annotated[CrawlJobService, Depends(get_crawl_job_service)]
CrawlNoteCopyServiceDep = Annotated[CrawlNoteCopyService, Depends(get_crawl_note_copy_service)]
CreatorServiceDep = Annotated[CreatorService, Depends(get_creator_service)]
CrawlCookieServiceDep = Annotated[CrawlCookieService, Depends(get_crawl_cookie_service)]
FinalcutCopyJobServiceDep = Annotated[FinalcutCopyJobService, Depends(get_finalcut_copy_job_service)]
FinalcutRenderJobServiceDep = Annotated[FinalcutRenderJobService, Depends(get_finalcut_render_job_service)]
