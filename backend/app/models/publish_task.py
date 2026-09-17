"""发布任务 ORM 模型。

任务模型采用「后端排队 + 客户端认领执行」的模式：
后端只负责任务的持久化与状态流转，实际的发布动作由 Electron 客户端认领后执行，
执行完毕回调上报结果。这样凭证始终留在客户端本地，后端不接触平台登录态。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.account import Account
from app.models.content import Content, utcnow


class PublishTaskStatus:
    """发布任务状态常量。"""

    PENDING = "pending"  # 排队中，等待客户端认领
    RUNNING = "running"  # 已被客户端认领，发布中
    SUCCESS = "success"  # 发布成功
    FAILED = "failed"  # 发布失败
    CANCELLED = "cancelled"  # 已取消

    ALL: tuple = (PENDING, RUNNING, SUCCESS, FAILED, CANCELLED)

    # 终态：不允许再发生状态流转（重试除外，重试是显式地把 failed 重置为 pending）
    TERMINAL: tuple = (SUCCESS, FAILED, CANCELLED)


class PublishTask(Base):
    """发布任务表。"""

    __tablename__ = "publish_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    content_id: Mapped[int] = mapped_column(
        ForeignKey("contents.id"), nullable=False, comment="待发布的内容 ID"
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id"), nullable=False, comment="目标账号 ID"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PublishTaskStatus.PENDING, comment="任务状态"
    )
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="计划执行时间（UTC），为空表示尽快执行"
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="实际开始时间（UTC）"
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="结束时间（UTC）"
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已重试次数"
    )
    max_retries: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, comment="最大重试次数"
    )
    error_message: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="失败原因"
    )
    result_url: Mapped[str] = mapped_column(
        String(1000), nullable=False, default="", comment="发布成功后的内容链接"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow, comment="更新时间（UTC）"
    )

    # 多对一关联：列表展示需要内容标题与账号昵称，这里预加载避免 N+1 查询。
    # 使用 selectin 而非 joined：joined 会生成 LEFT OUTER JOIN，
    # 与认领任务时的 SELECT ... FOR UPDATE 组合在 PostgreSQL 下会直接报错。
    content: Mapped[Content] = relationship("Content", lazy="selectin")
    account: Mapped[Account] = relationship("Account", lazy="selectin")

    __table_args__ = (
        Index("ix_publish_tasks_status", "status"),
        # 客户端认领时按「状态 + 计划时间」筛选，建立联合索引
        Index("ix_publish_tasks_status_scheduled", "status", "scheduled_at"),
        Index("ix_publish_tasks_account", "account_id"),
    )

    def __repr__(self) -> str:
        return f"<PublishTask id={self.id} status={self.status} content={self.content_id}>"
