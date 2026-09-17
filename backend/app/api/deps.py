"""FastAPI 依赖注入定义。

集中管理依赖类型别名，路由函数直接使用 Annotated 类型即可，
避免每个接口重复写 Depends(...)。
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.content_service import ContentService

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


# 内容服务依赖
ContentServiceDep = Annotated[ContentService, Depends(get_content_service)]
