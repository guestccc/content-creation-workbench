"""SQLAlchemy 声明式基类。

所有 ORM 模型继承 Base，统一放在 app/models 下，便于 Base.metadata 完整收集。
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """ORM 模型基类。"""
