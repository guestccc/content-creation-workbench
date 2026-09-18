"""字幕提取任务 ORM 模型。

形态与 SceneJob 完全一致（**这张表本身就是队列**）：一个后台工作线程按
status='pending' 的行依次执行，因此取消、重启恢复、worker 崩溃三件事都不需要
额外的同步代码 —— 取消就是改状态、重启后 pending 自动被接手、worker 死了任务
也还在表里。

状态常量在这里另立一份（值相同），而不是 import SceneJobStatus：两个功能
各有一套状态机，跨功能引用会把它们的演进焊死在一起。

一条任务覆盖一批视频，每条视频一行 SubtitleJobItem —— 一条失败不影响其它条，
界面上也能说清「哪条失败、为什么」。

与镜头分割的差别只有产物形态：那边是每条视频一个输出**目录**（装若干片段），
这边是每条视频一个输出**文件**（一个 .srt）。
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
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


class SubtitleJobStatus:
    """字幕提取任务状态常量。"""

    PENDING = "pending"  # 排队中，等待工作线程认领
    RUNNING = "running"  # 已认领，正在跑
    SUCCESS = "success"  # 全部视频转写成功
    PARTIAL = "partial"  # 部分视频失败 —— 整体算「完成但有错」
    FAILED = "failed"  # 全部视频都失败
    CANCELLED = "cancelled"  # 已取消

    ALL: tuple = (PENDING, RUNNING, SUCCESS, PARTIAL, FAILED, CANCELLED)

    # 终态：不允许再发生状态流转
    TERMINAL: tuple = (SUCCESS, PARTIAL, FAILED, CANCELLED)


class SubtitleJobItemStatus:
    """单条视频的处理状态常量。"""

    PENDING = "pending"  # 尚未轮到
    RUNNING = "running"  # 正在转写
    SUCCESS = "success"  # 转写成功（字幕文件已落盘）
    FAILED = "failed"  # 转写失败
    SKIPPED = "skipped"  # 任务被取消，该条未执行

    ALL: tuple = (PENDING, RUNNING, SUCCESS, FAILED, SKIPPED)
    TERMINAL: tuple = (SUCCESS, FAILED, SKIPPED)


class SubtitleJob(Base):
    """字幕提取任务表。"""

    __tablename__ = "subtitle_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SubtitleJobStatus.PENDING, comment="任务状态"
    )
    input_path: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="输入路径（视频文件或目录，已规范化为绝对路径）"
    )
    output_dir: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="字幕输出目录（已规范化为绝对路径）"
    )
    recursive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="输入为目录时是否递归子目录"
    )
    params: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="转写参数（白名单键：asr / language / format，见 services/subtitle_runner.py）",
    )

    # ---------- 进度字段：轮询接口直接读这些列，不重新计算 ----------
    total_videos: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="待处理视频总数"
    )
    completed_videos: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已处理完成的视频数（含失败）"
    )
    failed_videos: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="处理失败的视频数"
    )
    skipped_videos: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="因取消而跳过的视频数"
    )
    subtitle_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已产出的字幕文件数（节流更新）"
    )
    current_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="当前处理的视频序号（从 1 开始，0 表示未开始）"
    )
    current_video: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", comment="当前处理的视频文件名"
    )
    current_elapsed_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment=(
            "当前这条已跑了的秒数（节流更新）。转写没有机器可读的百分比可解析"
            "（VideoCaptioner 的进度条只在 stderr 是终端时才渲染），所以这个"
            "「已用时」是页面上唯一能如实给出的实时数字"
        ),
    )

    # ---------- 子进程句柄：取消、超时、孤儿回收共用的唯一依据 ----------
    child_pid: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="当前子进程组长 PID，为空表示没有在跑的子进程"
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="实际开始时间（UTC）"
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="结束时间（UTC）"
    )
    error_message: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="失败原因（多条失败时汇总）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow, comment="更新时间（UTC）"
    )

    # 一对多：详情页要按顺序展示每条视频的结果，用 selectin 预加载避免 N+1。
    items: Mapped[List["SubtitleJobItem"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="SubtitleJobItem.index",
    )

    __table_args__ = (
        Index("ix_subtitle_jobs_status", "status"),
        # 工作线程按「状态 + id」扫待执行任务，建立联合索引
        Index("ix_subtitle_jobs_status_id", "status", "id"),
        Index("ix_subtitle_jobs_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<SubtitleJob id={self.id} status={self.status}>"


class SubtitleJobItem(Base):
    """字幕提取任务中的单条视频。"""

    __tablename__ = "subtitle_job_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("subtitle_jobs.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属任务 ID",
    )
    index: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="在任务内的序号（从 1 开始），保证展示顺序稳定"
    )
    source_path: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="源视频绝对路径"
    )
    source_name: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="源视频文件名（冗余字段，便于展示）"
    )
    output_path: Mapped[str] = mapped_column(
        String(1000),
        nullable=False,
        comment="该条的字幕输出文件路径（创建任务时就定好，重名已加 -2/-3 后缀）",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=SubtitleJobItemStatus.PENDING,
        comment="该条的处理状态",
    )

    # ---------- 结果 ----------
    subtitle_exists: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="字幕文件是否真的落盘了"
    )
    file_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="字幕文件大小（字节）"
    )
    segment_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="字幕条数（解析 .srt 得到，解析不了则记 0）"
    )
    duration_seconds: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="源视频时长（秒），ffprobe 探测失败时为空"
    )
    exit_code: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="子进程退出码"
    )
    elapsed_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="该条耗时（秒）"
    )
    error_message: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="失败原因（通常是子进程日志的尾部）"
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="开始时间（UTC）"
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="结束时间（UTC）"
    )

    job: Mapped["SubtitleJob"] = relationship(back_populates="items")

    __table_args__ = (
        Index("ix_subtitle_job_items_job", "job_id"),
        Index("ix_subtitle_job_items_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<SubtitleJobItem id={self.id} job={self.job_id} name={self.source_name}>"
