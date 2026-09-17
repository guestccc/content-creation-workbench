"""内容 ORM 模型。"""

from datetime import datetime, timezone
from typing import List

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    """当前 UTC 时间（naive）。

    SQLite 的 DATETIME 不保存时区信息，这里统一存 naive UTC，
    在 API 层序列化时再补上 Z 后缀，保证前后端时区语义一致。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ContentStatus:
    """内容状态枚举常量。

    用字符串常量而非 Enum 类，便于后续扩展且与数据库存储值一致。
    """

    DRAFT = "draft"  # 草稿
    REVIEWING = "reviewing"  # 待审核
    PUBLISHED = "published"  # 已发布
    ARCHIVED = "archived"  # 已归档

    ALL: tuple = (DRAFT, REVIEWING, PUBLISHED, ARCHIVED)


class Content(Base):
    """创作内容表。"""

    __tablename__ = "contents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="标题")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", comment="正文")
    platform: Mapped[str] = mapped_column(
        String(50), nullable=False, default="", comment="目标平台，如抖音、小红书"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ContentStatus.DRAFT, comment="内容状态"
    )
    tags: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", comment="标签，逗号分隔存储"
    )
    author: Mapped[str] = mapped_column(String(100), nullable=False, default="", comment="作者")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow, comment="更新时间（UTC）"
    )

    # 列表页按状态、平台过滤是高频操作，建立索引
    __table_args__ = (
        Index("ix_contents_status", "status"),
        Index("ix_contents_platform", "platform"),
    )

    @property
    def tag_list(self) -> List[str]:
        """把逗号分隔的标签字符串还原为列表（对外输出用）。"""
        return [tag for tag in self.tags.split(",") if tag]

    def __repr__(self) -> str:
        return f"<Content id={self.id} title={self.title!r} status={self.status}>"
