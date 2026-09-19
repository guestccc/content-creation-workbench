"""一键成品任务的 ORM 模型：两个任务域各一张表。

与 MixJob 同构：**表本身就是队列**（`status='pending'` 的行被后台工作线程
依次认领），取消、重启恢复都不需要额外的同步代码。

为什么是两个任务域而不是一张带 `kind` 列的宽表：

- `FinalcutCopyJob`（文案生成）：一次 AI 调用，**没有子进程、没有磁盘产物**，
  进度是「读到哪一步」的相位（read → analyze → parse）；
- `FinalcutRenderJob`（合成烧字）：N 条 ffmpeg 子进程，每条成片一行
  `FinalcutRenderItem`，有 `child_pid`、有产物目录、有孤儿回收。

两者的状态语义、取消语义、删除语义（有无 purge）完全不同，塞进一张表会让
每一列都得解释「这个字段对哪种 kind 有意义」。四个既有任务域都是
「model + schema + service + runner + worker」五件套，保持一致。

两个域之间是**弱关联**：渲染任务存 `copy_job_id` 仅用于溯源（SET NULL），
文案内容在每条 item 上存了快照（`copy_text`）—— 删掉文案任务不影响成片
记录，重新生成一批文案也不会改动已在合成队列里的东西。
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.content import utcnow


class FinalcutCopyJobStatus:
    """文案任务状态常量。"""

    PENDING = "pending"  # 排队中，等待工作线程认领
    RUNNING = "running"  # 已认领，正在调 AI
    SUCCESS = "success"  # 文案已生成并解析入库
    FAILED = "failed"  # 失败（原因在 error_message，含 AI 错误分类后的可读文案）
    CANCELLED = "cancelled"  # 已取消（结果不落库）

    ALL: tuple = (PENDING, RUNNING, SUCCESS, FAILED, CANCELLED)
    TERMINAL: tuple = (SUCCESS, FAILED, CANCELLED)


class FinalcutRenderJobStatus:
    """合成任务状态常量（与混剪同一套语义）。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"  # 全部成片成功
    PARTIAL = "partial"  # 部分成片失败 —— 整体算「完成但有错」
    FAILED = "failed"  # 全部失败（含环境类前置失败）
    CANCELLED = "cancelled"

    ALL: tuple = (PENDING, RUNNING, SUCCESS, PARTIAL, FAILED, CANCELLED)
    TERMINAL: tuple = (SUCCESS, PARTIAL, FAILED, CANCELLED)


class FinalcutItemStatus:
    """单条成片的状态常量。"""

    PENDING = "pending"  # 尚未轮到
    RUNNING = "running"  # 正在烧字
    SUCCESS = "success"  # 文件已就位
    FAILED = "failed"
    SKIPPED = "skipped"  # 任务被取消，未执行

    ALL: tuple = (PENDING, RUNNING, SUCCESS, FAILED, SKIPPED)
    TERMINAL: tuple = (SUCCESS, FAILED, SKIPPED)


class FinalcutCopyPhase:
    """文案任务的相位（进度展示用）。

    文案任务没有子进程进度可读，能如实报告的只有「走到哪一步了」：
    读字幕 → 调 AI → 解析结果。取消检查也只在相位边界发生（在途 HTTP
    无法即时中断，上界是 AI_TIMEOUT_SECONDS，见设计文档）。
    """

    READ = "read"  # 读取并拆解字幕素材
    ANALYZE = "analyze"  # 调用 AI 生成文案
    PARSE = "parse"  # 解析与校验 AI 返回

    ALL: tuple = (READ, ANALYZE, PARSE)


class FinalcutCopyJob(Base):
    """一键成品的文案生成任务（AI 调用，无子进程无磁盘产物）。"""

    __tablename__ = "finalcut_copy_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FinalcutCopyJobStatus.PENDING, comment="任务状态"
    )

    # ---------- 输入（创建时校验并固化，之后只认记录里的路径）----------
    subtitle_path: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="字幕素材绝对路径（.srt/.ass/.vtt）"
    )
    video_path: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="成片视频绝对路径（定长依据）"
    )
    video_duration: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, comment="视频时长（秒），AI 定量用"
    )
    copy_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, comment="要生成几条候选文案"
    )
    hint: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="用户补充提示（卖点/受众/口播偏好）"
    )
    model: Mapped[str] = mapped_column(
        String(100), nullable=False, default="", comment="实际使用的模型名（快照）"
    )

    # ---------- 结果 ----------
    result: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True,
        comment="解析后的产物 {analysis, copies}，结构与 schemas/finalcut_job.py 一致",
    )
    raw_response: Mapped[str] = mapped_column(
        Text, nullable=False, default="",
        comment="AI 返回原文。解析失败时是唯一的排查依据，必须留",
    )
    tokens_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="本次调用的 total_tokens"
    )

    # ---------- 进度（相位 + 百分比；无子进程可读，相位就是真实进度）----------
    current_phase: Mapped[str] = mapped_column(
        String(20), nullable=False, default="", comment="当前相位：read/analyze/parse"
    )
    progress_percent: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, comment="完成百分比（相位推进时跳变）"
    )

    error_message: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="失败原因（已按 AI 错误分类翻译成人话）"
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="实际开始时间（UTC）"
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="结束时间（UTC）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow, comment="更新时间（UTC）"
    )

    __table_args__ = (
        Index("ix_finalcut_copy_jobs_status", "status"),
        Index("ix_finalcut_copy_jobs_status_id", "status", "id"),
        Index("ix_finalcut_copy_jobs_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<FinalcutCopyJob id={self.id} status={self.status}>"


class FinalcutRenderJob(Base):
    """一键成品的合成任务（把勾选的文案烧进视频，每条候选一个成片）。"""

    __tablename__ = "finalcut_render_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FinalcutRenderJobStatus.PENDING, comment="任务状态"
    )

    # ---------- 溯源与输入 ----------
    copy_job_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("finalcut_copy_jobs.id", ondelete="SET NULL"),
        nullable=True,
        comment="来源文案任务（仅溯源；item 上有文案快照，删了它不影响本任务）",
    )
    video_path: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="成片视频绝对路径（创建时校验并固化）"
    )
    video_duration: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, comment="视频时长（秒）"
    )
    video_spec: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict,
        comment="视频规格 {width,height,fps_*,rotation,has_audio,audio_codec}（显示尺寸）",
    )
    output_dir: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="产物目录（materials/finalcut/finalcut-<时间戳>/）"
    )
    font_file: Mapped[str] = mapped_column(
        String(1000), nullable=False, default="", comment="烧字用的字体文件（创建时探测快照）"
    )

    # ---------- 进度字段：轮询接口直接读这些列 ----------
    total_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="待产出的成片总数"
    )
    completed_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已完成的成片数（含失败）"
    )
    failed_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="失败的成片数"
    )
    skipped_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="因取消而跳过的成片数"
    )
    current_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="当前处理的成片序号（从 1 开始，0 表示未开始）"
    )
    progress_percent: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, comment="当前这条成片的完成百分比（0-100）"
    )

    # ---------- 子进程句柄：取消、超时、孤儿回收共用的唯一依据 ----------
    child_pid: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="当前 ffmpeg 进程组长 PID，为空表示没有在跑的子进程"
    )

    error_message: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="失败原因（多条失败时汇总）"
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="实际开始时间（UTC）"
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="结束时间（UTC）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow, comment="更新时间（UTC）"
    )

    # 一对多：详情页要按顺序展示每条成片，用 selectin 预加载避免 N+1。
    items: Mapped[List["FinalcutRenderItem"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="FinalcutRenderItem.index",
    )

    __table_args__ = (
        Index("ix_finalcut_render_jobs_status", "status"),
        Index("ix_finalcut_render_jobs_status_id", "status", "id"),
        Index("ix_finalcut_render_jobs_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<FinalcutRenderJob id={self.id} status={self.status}>"


class FinalcutRenderItem(Base):
    """合成任务中的一条成片（一条文案 × 一个框 × 一套样式）。"""

    __tablename__ = "finalcut_render_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("finalcut_render_jobs.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属任务 ID",
    )
    index: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="成片序号（从 1 开始），保证展示顺序稳定"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FinalcutItemStatus.PENDING, comment="该条成片的状态"
    )

    # ---------- 文案快照与排版参数 ----------
    copy_text: Mapped[str] = mapped_column(
        Text, nullable=False, comment="文案快照（用户可能改过字；不随文案任务变化）"
    )
    angle: Mapped[str] = mapped_column(
        String(100), nullable=False, default="", comment="文案角度（快照，展示用）"
    )
    style: Mapped[str] = mapped_column(
        String(30), nullable=False, default="white_box",
        comment="上屏样式模板 key（见 finalcut_render.TEXT_STYLES）",
    )
    box: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict,
        comment="框选区域 {x,y,w,h}，归一化 0-1（相对显示尺寸）",
    )
    font_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="请求字号，0 = 自动"
    )
    resolved_font_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="实际使用的字号（自适应计算结果，展示用）"
    )

    # ---------- 结果 ----------
    output_path: Mapped[str] = mapped_column(
        String(1000), nullable=False, default="", comment="成片绝对路径（成功后才填）"
    )
    output_name: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", comment="成片文件名（冗余字段，便于展示）"
    )
    duration_seconds: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="成片时长（秒）"
    )
    size_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="成片文件大小（字节）"
    )
    exit_code: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="子进程退出码"
    )
    elapsed_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="该条成片耗时（秒）"
    )
    error_message: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="失败原因（通常是 ffmpeg 日志尾部）"
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="开始时间（UTC）"
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="完成时间（UTC）"
    )

    job: Mapped["FinalcutRenderJob"] = relationship(back_populates="items")

    __table_args__ = (
        Index("ix_finalcut_render_items_job", "job_id"),
        Index("ix_finalcut_render_items_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<FinalcutRenderItem id={self.id} job={self.job_id} index={self.index}>"
