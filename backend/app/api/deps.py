"""FastAPI 依赖注入定义。

集中管理依赖类型别名，路由函数直接使用 Annotated 类型即可，
避免每个接口重复写 Depends(...)。
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.account_service import AccountService
from app.services.content_service import ContentService
from app.services.publish_task_service import PublishTaskService

# 数据库会话依赖
DbSession = Annotated[Session, Depends(get_db)]


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


# 服务依赖类型别名
ContentServiceDep = Annotated[ContentService, Depends(get_content_service)]
AccountServiceDep = Annotated[AccountService, Depends(get_account_service)]
PublishTaskServiceDep = Annotated[PublishTaskService, Depends(get_publish_task_service)]
