"""平台账号 ORM 模型。

安全说明：
本表**只存储账号的元信息**（平台、昵称、状态等）。
登录凭证（Cookie / Token）不经过后端、不入库，由 Electron 客户端使用
系统级安全存储（Electron safeStorage，macOS 下走 Keychain）加密后保存在本机。
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.content import utcnow


class AccountStatus:
    """账号状态常量。"""

    ACTIVE = "active"  # 正常可用
    DISABLED = "disabled"  # 已停用
    EXPIRED = "expired"  # 登录态失效，需重新授权

    ALL: tuple = (ACTIVE, DISABLED, EXPIRED)


class Account(Base):
    """平台账号表。"""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    platform: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="所属平台，如抖音、小红书"
    )
    nickname: Mapped[str] = mapped_column(String(100), nullable=False, comment="账号昵称")
    account_uid: Mapped[str] = mapped_column(
        String(100), nullable=False, default="", comment="平台侧账号 ID，可留空"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AccountStatus.ACTIVE, comment="账号状态"
    )
    remark: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", comment="备注"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow, comment="更新时间（UTC）"
    )

    __table_args__ = (
        # 同一平台下的账号昵称唯一，避免重复录入造成发布错账号
        UniqueConstraint("platform", "nickname", name="uq_accounts_platform_nickname"),
        Index("ix_accounts_platform", "platform"),
        Index("ix_accounts_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<Account id={self.id} platform={self.platform!r} nickname={self.nickname!r}>"
