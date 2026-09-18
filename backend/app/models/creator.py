"""创作者主页 ORM 模型。

素材抓取「创作者主页」模式的素材库：按平台维护常抓的创作者。
homepage 字段就是抓取时透传给 MC --creator_id 的值 —— 主页链接或纯 ID
都行（各平台能吃什么见前端 PLATFORM_SPECS 的 creatorHelp，抓取失败的
错误会出现在任务日志尾部）。
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.content import utcnow


class Creator(Base):
    """创作者主页表。"""

    __tablename__ = "creators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    platform: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="平台标识（CrawlPlatform.ALL）"
    )
    name: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="展示名（博主昵称或自起名）"
    )
    homepage: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="主页链接或 ID（透传给 MC --creator_id）"
    )
    tags: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, comment="标签列表（自由输入，如母婴/美食）"
    )
    remark: Mapped[str] = mapped_column(String(200), nullable=False, default="", comment="备注")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow, comment="更新时间（UTC）"
    )

    __table_args__ = (
        # 同一平台下主页唯一：主页就是抓取时的身份标识，重复录入没有意义
        UniqueConstraint("platform", "homepage", name="uq_creators_platform_homepage"),
        Index("ix_creators_platform", "platform"),
    )

    def __repr__(self) -> str:
        return f"<Creator id={self.id} platform={self.platform!r} name={self.name!r}>"
