"""镜头分割任务 ORM 模型。

与 PublishTask 的「后端排队 + 客户端认领执行」不同，镜头分割任务的执行者
就是后端自己：一个后台工作线程按 DB 里 status='pending' 的行依次执行。

因此这里**没有单独的队列数据结构** —— 这张表本身就是队列。
好处是取消、重启恢复、worker 线程崩溃这三件事全都不需要额外的同步代码：
取消 pending 任务就是改状态（worker 读到时会跳过）；重启后 pending 任务
自动被接手；worker 线程死了任务也还在表里。

任务分两种模式：
- preview：只跑检测、把切点清单存进 item.scenes，让用户先看效果；
- split：检测之后真正切出片段文件。

一条任务可以覆盖多个视频（输入是目录时），每个视频一行 SceneJobItem，
这样一个视频失败不会影响其它视频，界面上也能说清「哪条失败了、为什么」。
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


class SceneJobStatus:
    """镜头分割任务状态常量。"""

    PENDING = "pending"  # 排队中，等待工作线程认领
    RUNNING = "running"  # 已认领，正在跑
    SUCCESS = "success"  # 全部视频处理成功
    PARTIAL = "partial"  # 部分视频失败 —— 整体算「完成但有错」
    FAILED = "failed"  # 全部视频都失败
    CANCELLED = "cancelled"  # 已取消

    ALL: tuple = (PENDING, RUNNING, SUCCESS, PARTIAL, FAILED, CANCELLED)

    # 终态：不允许再发生状态流转（「只重试失败项」是显式地把 failed 的条目重置）
    TERMINAL: tuple = (SUCCESS, PARTIAL, FAILED, CANCELLED)


class SceneJobItemStatus:
    """单个视频的处理状态常量。"""

    PENDING = "pending"  # 尚未轮到
    RUNNING = "running"  # 正在处理
    SUCCESS = "success"  # 处理成功（含「只有一个镜头」这种合法但零片段的结果）
    FAILED = "failed"  # 处理失败
    SKIPPED = "skipped"  # 任务被取消，该条未执行

    ALL: tuple = (PENDING, RUNNING, SUCCESS, FAILED, SKIPPED)
    TERMINAL: tuple = (SUCCESS, FAILED, SKIPPED)


class SceneJobMode:
    """任务模式常量。"""

    PREVIEW = "preview"  # 只检测切点，不切文件
    SPLIT = "split"  # 检测并切出片段

    ALL: tuple = (PREVIEW, SPLIT)


class SceneJob(Base):
    """镜头分割任务表。"""

    __tablename__ = "scene_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SceneJobMode.SPLIT, comment="任务模式：preview / split"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SceneJobStatus.PENDING, comment="任务状态"
    )
    input_path: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="输入路径（视频文件或目录，已规范化为绝对路径）"
    )
    output_dir: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="输出根目录（已规范化为绝对路径）"
    )
    recursive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="输入为目录时是否递归子目录"
    )
    params: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, comment="检测参数（白名单键，见 core/scene_templates.py）"
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
    clip_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已切出的片段总数（节流更新）"
    )
    scene_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已检测出的镜头总数（节流更新）"
    )
    current_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="当前处理的视频序号（从 1 开始，0 表示未开始）"
    )
    current_video: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", comment="当前处理的视频文件名"
    )
    current_clips: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="当前视频已处理到的片段序号（vct 逐段上报）"
    )
    current_clip_names: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, comment="当前视频已切出的片段文件名（供实时预览）"
    )
    current_phase: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="",
        comment="当前视频所处阶段：detect 检测中 / split 切割中，空表示还没收到 vct 的上报",
    )
    current_total_clips: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="当前视频预计切出的片段数。检测跑完才知道分母，之前恒为 0（那时只能显示「检测中」）",
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

    # 一对多：详情页要按顺序展示每个视频的结果，用 selectin 预加载避免 N+1。
    items: Mapped[List["SceneJobItem"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="SceneJobItem.index",
    )

    __table_args__ = (
        Index("ix_scene_jobs_status", "status"),
        # 工作线程按「状态 + id」扫待执行任务，建立联合索引
        Index("ix_scene_jobs_status_id", "status", "id"),
        Index("ix_scene_jobs_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<SceneJob id={self.id} mode={self.mode} status={self.status}>"


class SceneJobItem(Base):
    """镜头分割任务中的单个视频。"""

    __tablename__ = "scene_job_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("scene_jobs.id", ondelete="CASCADE"),
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
    output_dir: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="该视频的输出目录（split 模式为 <输出根目录>/<视频名>/）"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=SceneJobItemStatus.PENDING,
        comment="该视频的处理状态",
    )

    # ---------- 结果 ----------
    scene_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="检测出的镜头数"
    )
    scenes: Mapped[Optional[list]] = mapped_column(
        JSON, nullable=True, comment="切点清单 [{number,start,end,duration}]，预览模式的主要产物"
    )
    clip_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="实际切出的片段数"
    )
    clip_names: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, comment="切出的片段文件名列表"
    )
    failed_clip_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="切割失败的片段数（vct 退出码 5 可能只是部分失败，据此区分）",
    )
    single_shot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        comment="是否为单镜头视频（全片无画面跳变，此时切不出片段是正常结果）",
    )
    duration_seconds: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="源视频时长（秒），ffprobe 探测失败时为空"
    )
    exit_code: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="子进程退出码"
    )
    elapsed_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="该视频耗时（秒）"
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

    job: Mapped["SceneJob"] = relationship(back_populates="items")

    __table_args__ = (
        Index("ix_scene_job_items_job", "job_id"),
        Index("ix_scene_job_items_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<SceneJobItem id={self.id} job={self.job_id} name={self.source_name}>"
