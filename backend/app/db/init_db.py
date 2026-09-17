"""数据库初始化。

开发阶段直接用 create_all 建表；生产环境应改用 Alembic 迁移管理表结构变更。
"""

from app.core.logging import get_logger
from app.db.base import Base
from app.db.session import engine

# 导入模型包，确保所有 ORM 类都注册到 Base.metadata 上
import app.models  # noqa: F401

logger = get_logger(__name__)


def init_db() -> None:
    """创建所有尚不存在的数据表（已存在的表不会被修改）。"""
    Base.metadata.create_all(bind=engine)
    logger.info("数据库表结构初始化完成")
