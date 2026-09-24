"""素材抓取笔记的 AI 文案生成结果表。

一条（抓取任务, 笔记）只留最新一份生成结果：「换一批」是覆盖语义，
不是历史版本 —— 用户要的是「这条笔记配什么文案」，不是版本管理。

**不建外键**：抓取任务被删（连同产物）是常规操作，级联由 service 层在
同一事务里做（crawl_job_service.delete_job/delete_jobs →
crawl_note_copy_service.delete_copies_for_jobs）；笔记 id 是各平台原生的
字符串，本来也建不了外键 —— 与 background_jobs.source_crawl_job_id 同一口径。
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.content import utcnow


class CrawlNoteAiCopy(Base):
    """一条笔记的 AI 文案（小红书 + 抖音各一份，存 payload JSON）。"""

    __tablename__ = "crawl_note_ai_copies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    crawl_job_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="来源抓取任务 id（仅关联不建外键：任务删除走 service 层同事务级联）",
    )
    note_id: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="平台原生笔记 id（字符串，建不了外键，同 background_jobs.source_crawl_note_id）",
    )
    payload: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        comment='生成结果：{"xhs": {"titles": [...至多5], "intros": [...至多3]}, "dy": {...}}',
    )
    model: Mapped[str] = mapped_column(
        String(100), nullable=False, default="", comment="生成用的模型名（排查/展示用）"
    )
    tokens_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="本次生成的 token 用量（usage.total_tokens）"
    )
    raw_response: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        comment="模型原文（解析失败排查全靠它，同 finalcut_copy_jobs.raw_response）",
    )
    reasoning: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        comment="模型思维链原文（DeepSeek 思考模式的 reasoning_content），供弹窗回看",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow, comment="更新时间（UTC）"
    )

    __table_args__ = (
        # 一条（任务, 笔记）只留一份：「换一批」是覆盖，靠这个唯一约束兜住并发双击
        UniqueConstraint("crawl_job_id", "note_id", name="uq_crawl_note_ai_copy"),
        Index("ix_crawl_note_ai_copies_job", "crawl_job_id"),
    )

    def __repr__(self) -> str:
        return f"<CrawlNoteAiCopy id={self.id} job={self.crawl_job_id} note={self.note_id}>"
