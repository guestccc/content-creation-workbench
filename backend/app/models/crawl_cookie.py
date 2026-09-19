"""Cookie 库 ORM 模型。

素材抓取「Cookie 登录」的凭据库：按平台保存常用的登录 Cookie，
抓取时从库里选取，免去每次手工粘贴。

与 CrawlJob.login_cookies 的区别：那里是单次任务的快照（用完即弃），
这里是用户主动维护的可复用凭据。
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.content import utcnow


class CrawlCookie(Base):
    """Cookie 库表。"""

    __tablename__ = "crawl_cookies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    platform: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="平台标识（CrawlPlatform.ALL）"
    )
    name: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="展示名（如「主号」「小号」）"
    )
    cookie: Mapped[str] = mapped_column(
        Text, nullable=False, comment="登录 Cookie 串（敏感凭据，仅用于抓取时透传）"
    )
    remark: Mapped[str] = mapped_column(String(200), nullable=False, default="", comment="备注")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow, comment="更新时间（UTC）"
    )

    __table_args__ = (
        # 同一平台下名称唯一：名字就是用户辨认的标识，重复没有区分意义
        UniqueConstraint("platform", "name", name="uq_crawl_cookies_platform_name"),
        Index("ix_crawl_cookies_platform", "platform"),
    )

    def __repr__(self) -> str:
        return f"<CrawlCookie id={self.id} platform={self.platform!r} name={self.name!r}>"
