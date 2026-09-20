"""智能混剪任务 ORM 模型。

与 SceneJob 同构：**这张表本身就是队列** —— `status='pending'` 的行会被
后台工作线程依次认领。于是取消、重启恢复、worker 崩溃这三件事全都不需要
额外的同步代码（理由与 scene_job.py 顶部一致，不再重复）。

一条任务产出 N 条成片，每个成片一行 MixJobItem。三条素材清单存在任务上：

- `opening` / `ending`：用户选定的顺序，原样使用；
- `middle`：用户选定的一批，**顺序无意义** —— 每条成片各自打乱一次。

因此「这一条的中间段到底怎么排的」必须落在 MixJobItem 上，不能从任务反推。
`MixJobItem.order` 存的就是该条成片的**完整最终顺序**（开头 + 打乱后的中间 + 结尾），
配合任务上的 `seed`，这个任务在任何时候都能被完整复现与复盘。

素材一律存**相对 materials 根目录的路径**（如
`clips/xxx_scenes/xxx_clip_001.mp4`）而不是绝对路径：素材盘换了挂载点、
`SCENE_MATERIALS_DIR` 改了位置，历史记录仍然可读、可复现。
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
from app.models.job_common import JobRemarkMixin


class MixJobStatus:
    """混剪任务状态常量。"""

    PENDING = "pending"  # 排队中，等待工作线程认领
    RUNNING = "running"  # 已认领，正在跑
    SUCCESS = "success"  # 全部成片成功
    PARTIAL = "partial"  # 部分成片失败 —— 整体算「完成但有错」
    FAILED = "failed"  # 全部成片都失败
    CANCELLED = "cancelled"  # 已取消

    ALL: tuple = (PENDING, RUNNING, SUCCESS, PARTIAL, FAILED, CANCELLED)

    # 终态：不允许再发生状态流转
    TERMINAL: tuple = (SUCCESS, PARTIAL, FAILED, CANCELLED)


class MixOutputStatus:
    """单条成片的状态常量。"""

    PENDING = "pending"  # 尚未轮到
    RUNNING = "running"  # 正在拼接
    SUCCESS = "success"  # 拼接成功，文件已就位
    FAILED = "failed"  # 拼接失败
    SKIPPED = "skipped"  # 任务被取消，或所依赖的片段归一化失败，未执行

    ALL: tuple = (PENDING, RUNNING, SUCCESS, FAILED, SKIPPED)
    TERMINAL: tuple = (SUCCESS, FAILED, SKIPPED)


class MixPhase:
    """任务当前所处阶段（归一化 → 拼接）。

    两遍编码的产物：第一阶段逐个片段归一化（慢，占绝大部分耗时），
    第二阶段逐条成片流复制拼接（快）。分开报进度，界面上才不会出现
    「进度条卡在 90% 不动」这种假象。
    """

    NORMALIZE = "normalize"  # 逐片段归一化
    CONCAT = "concat"  # 流复制拼接成片

    ALL: tuple = (NORMALIZE, CONCAT)


class MixJob(Base, JobRemarkMixin):
    """智能混剪任务表。"""

    __tablename__ = "mix_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MixJobStatus.PENDING, comment="任务状态"
    )

    # ---------- 三段素材清单（相对 materials 根的路径）----------
    opening: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, comment="开头片段清单，按用户选择的顺序"
    )
    middle: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, comment="中间片段清单，顺序无意义（每条成片各自打乱）"
    )
    ending: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, comment="结尾片段清单，按用户选择的顺序"
    )

    # ---------- 合成参数 ----------
    count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="要产出几条成片"
    )
    output_dir: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="成片输出根目录（绝对路径）"
    )
    seed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        comment="本任务的随机种子。同一个种子能复现完全相同的 N 条中间段顺序",
    )
    target: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict,
        comment="成片规格 {width,height,fps_num,fps_den}，取自开头清单的第一条片段",
    )

    # ---------- 进度字段：轮询接口直接读这些列，不重新计算 ----------
    total_outputs: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="待产出的成片总数"
    )
    completed_outputs: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已完成的成片数（含失败）"
    )
    failed_outputs: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="失败的成片数"
    )
    skipped_outputs: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="因取消或依赖片段失败而跳过的成片数"
    )
    total_clips: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="需要归一化的片段总数（去重后）"
    )
    done_clips: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已归一化完成的片段数（含失败）"
    )
    current_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="当前处理的成片序号（从 1 开始，0 表示未开始）"
    )
    current_clip: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", comment="当前正在归一化的片段文件名"
    )
    current_phase: Mapped[str] = mapped_column(
        String(20), nullable=False, default="",
        comment="当前阶段：normalize 归一化中 / concat 拼接中，空表示还没开始",
    )
    progress_percent: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0,
        comment="当前阶段内部的完成百分比（0-100）。分母真实存在，不编造权重",
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

    # 一对多：详情页要按顺序展示每条成片，用 selectin 预加载避免 N+1。
    outputs: Mapped[List["MixJobItem"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MixJobItem.index",
    )

    __table_args__ = (
        Index("ix_mix_jobs_status", "status"),
        # 工作线程按「状态 + id」扫待执行任务，建立联合索引
        Index("ix_mix_jobs_status_id", "status", "id"),
        Index("ix_mix_jobs_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<MixJob id={self.id} status={self.status} count={self.count}>"


class MixJobItem(Base):
    """混剪任务中的一条成片。"""

    __tablename__ = "mix_job_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("mix_jobs.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属任务 ID",
    )
    index: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="成片序号（从 1 开始），保证展示顺序稳定"
    )
    order: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list,
        comment="该条成片的完整最终顺序（开头 + 打乱后的中间 + 结尾），相对 materials 根",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MixOutputStatus.PENDING, comment="该条成片的状态"
    )

    # ---------- 结果 ----------
    output_path: Mapped[str] = mapped_column(
        String(1000), nullable=False, default="", comment="成片绝对路径（成功后才填）"
    )
    output_name: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", comment="成片文件名（冗余字段，便于展示）"
    )
    duration_seconds: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, comment="成片时长（秒）= 各段时长之和"
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

    job: Mapped["MixJob"] = relationship(back_populates="outputs")

    __table_args__ = (
        Index("ix_mix_job_items_job", "job_id"),
        Index("ix_mix_job_items_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<MixJobItem id={self.id} job={self.job_id} index={self.index}>"
