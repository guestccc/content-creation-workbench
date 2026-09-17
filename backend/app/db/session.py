"""数据库引擎、会话与事务管理。

要点：
1. SQLite 需要 check_same_thread=False 才能在 FastAPI 线程池中跨线程使用；
2. get_db 作为 FastAPI 依赖，负责会话的创建与释放；
3. 写操作统一通过 transaction() 上下文管理器保证事务语义。
"""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# SQLite 在多线程场景下的连接参数；其他数据库无需此配置
_connect_args: dict = {}
if settings.DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine: Engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    connect_args=_connect_args,
    # 连接失效时自动重连，避免连接被回收后首次请求报错
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    # 关闭提交后过期，避免 commit 之后再访问属性触发额外查询
    expire_on_commit=False,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
    """SQLite 默认不校验外键，这里显式打开以保证数据一致性。"""
    if settings.DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：提供数据库会话，请求结束后自动关闭。

    出现未捕获异常时先回滚，避免把脏会话归还。
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def transaction(db: Session) -> Iterator[Session]:
    """显式事务上下文管理器。

    正常退出时提交，抛出任何异常时整体回滚并向上传递。
    这是所有写操作的唯一入口，确保不会出现写了一半的数据。

    Args:
        db: 数据库会话。

    Yields:
        同一个会话对象，便于在 with 块内继续操作。
    """
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("事务已回滚", exc_info=True)
        raise
