"""一键换背景任务 ORM 模型。

形态与 SubtitleJob 一致（**这张表本身就是队列**）：一个后台工作线程按
status='pending' 的行依次执行，因此取消、重启恢复、worker 崩溃三件事都不需要
额外的同步代码 —— 取消就是改状态、重启后 pending 自动被接手、worker 死了任务
也还在表里。

状态常量在这里另立一份（值相同），而不是 import SubtitleJobStatus：两个功能
各有一套状态机，跨功能引用会把它们的演进焊死在一起。

一条任务覆盖一批原图，每张原图一行 BackgroundJobItem —— 一张失败不影响其它张，
界面上也能说清「哪张失败、为什么」。

**与 subtitle 的结构性差别（不是抄漏的）**：

1. **没有 `child_pid`**。抠图是纯 numpy 运算，跑在 worker 线程自己的进程里，
   没有子进程可杀。所以取消不是「杀掉正在跑的东西」，而是「等这一张算完再收手」——
   取消延迟的上界 = 单张图的处理时间，见 services/background_runner.py 的说明。
2. **条目带 `stats` JSON 列**。算法会产出一组诊断数字（墨色 / 纸色 / 定位线档位 /
   位置门抠掉多少像素 / 暖色抑制抠掉多少像素 / 可见占比 / warnings）。这个算法的
   典型失败模式（定位线把背景圈进来）光看产出的 PNG 很难判断，有这组数字才有救，
   所以它是**结果的一部分**，必须落库，不能只写日志。
3. **没有 `exit_code` / `subtitle_exists`**：没有子进程就谈不上退出码；判成功靠
   「本张无异常 + 产物文件存在且非空」（见 runner）。
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    JSON,
    DateTime,
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


class BackgroundJobStatus:
    """一键换背景任务状态常量。"""

    PENDING = "pending"  # 排队中，等待工作线程认领
    RUNNING = "running"  # 已认领，正在跑
    SUCCESS = "success"  # 全部原图换底成功
    PARTIAL = "partial"  # 部分原图失败 —— 整体算「完成但有错」
    FAILED = "failed"  # 全部原图都失败
    CANCELLED = "cancelled"  # 已取消

    ALL: tuple = (PENDING, RUNNING, SUCCESS, PARTIAL, FAILED, CANCELLED)

    # 终态：不允许再发生状态流转
    TERMINAL: tuple = (SUCCESS, PARTIAL, FAILED, CANCELLED)


class BackgroundJobItemStatus:
    """单张原图的处理状态常量。"""

    PENDING = "pending"  # 尚未轮到
    RUNNING = "running"  # 正在抠图 / 合成
    SUCCESS = "success"  # 换底成功（产物 PNG 已落盘）
    FAILED = "failed"  # 该张失败（读图失败、超像素上限、贴纸比背景大等）
    SKIPPED = "skipped"  # 任务被取消，该张未执行

    ALL: tuple = (PENDING, RUNNING, SUCCESS, FAILED, SKIPPED)
    TERMINAL: tuple = (SUCCESS, FAILED, SKIPPED)

    # 可重试 = 「没产出」的那两种：failed 是跑了但失败，skipped 是压根没轮到
    # （取消 / 服务重启）。成功的重跑没有意义，pending / running 的正在被跑。
    RETRYABLE: tuple = (FAILED, SKIPPED)


class BackgroundJob(Base, JobRemarkMixin):
    """一键换背景任务表。"""

    __tablename__ = "background_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BackgroundJobStatus.PENDING, comment="任务状态"
    )

    # ---------- 来源（跨域弱关联，仅溯源） ----------
    # 原图常常是从素材抓取的结果里带过来的。记下来源只为一件事：回过头能说清
    # 「这条换背景任务是拿哪条抓取任务的哪条笔记跑出来的」。
    # **不建外键**：抓取任务被删（连同产物）是常规操作，不该反过来影响已有记录；
    # 笔记 id 是各平台原生的字符串，本来也建不了外键 —— 一半有约束一半没有，
    # 比整对都松更糟（同 finalcut_render_jobs.copy_job_id 的「仅溯源」口径）。
    source_crawl_job_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="来源素材抓取任务 id（仅溯源，不建外键）"
    )
    source_crawl_note_id: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        default="",
        comment=(
            "来源笔记 id（抓取产物 <平台>/images/<笔记 id>/ 的目录名）；"
            "空串表示不是从抓取结果带过来的"
        ),
    )

    input_path: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="原图输入路径（文件或目录，已规范化为绝对路径）"
    )
    output_dir: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="产物输出目录（已规范化为绝对路径）"
    )
    files: Mapped[list] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        comment="勾选的原图文件名清单（纯文件名，不含路径分隔符）；为空表示处理目录下全部图片",
    )
    background_path: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="背景图绝对路径（单张）"
    )
    params: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment="算法参数（白名单键，见 services/cutout.py 的 CutoutParams）",
    )

    # ---------- 进度字段：轮询接口直接读这些列，不重新计算 ----------
    total_images: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="待处理原图总数"
    )
    completed_images: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已处理完成的原图数（含失败）"
    )
    failed_images: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="处理失败的原图数"
    )
    skipped_images: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="因取消而跳过的原图数"
    )
    current_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="当前处理的原图序号（从 1 开始，0 表示未开始）"
    )
    current_image: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", comment="当前处理的原图文件名"
    )
    current_elapsed_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment=(
            "当前这张已跑了的秒数（收尾时更新）。抠图是纯 numpy 运算，没有中间"
            "进度可上报，所以「已用时」是页面上唯一能如实给出的实时数字"
        ),
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="实际开始时间（UTC）"
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="结束时间（UTC）"
    )
    error_message: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="失败原因（多张失败时汇总）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow, comment="更新时间（UTC）"
    )

    # 一对多：详情页要按顺序展示每张原图的结果，用 selectin 预加载避免 N+1。
    items: Mapped[List["BackgroundJobItem"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="BackgroundJobItem.index",
    )

    __table_args__ = (
        Index("ix_background_jobs_status", "status"),
        # 工作线程按「状态 + id」扫待执行任务，建立联合索引
        Index("ix_background_jobs_status_id", "status", "id"),
        Index("ix_background_jobs_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<BackgroundJob id={self.id} status={self.status}>"


class BackgroundJobItem(Base):
    """一键换背景任务中的单张原图。"""

    __tablename__ = "background_job_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    job_id: Mapped[int] = mapped_column(
        ForeignKey("background_jobs.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属任务 ID",
    )
    index: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="在任务内的序号（从 1 开始），保证展示顺序稳定"
    )
    source_path: Mapped[str] = mapped_column(
        String(1000), nullable=False, comment="源原图绝对路径"
    )
    source_name: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="源原图文件名（冗余字段，便于展示）"
    )
    output_path: Mapped[str] = mapped_column(
        String(1000),
        nullable=False,
        comment="该张的产物 PNG 路径（创建任务时就定好，重名已加 -2/-3 后缀）",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=BackgroundJobItemStatus.PENDING,
        comment="该张的处理状态",
    )

    # ---------- 结果 ----------
    stats: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment=(
            "该张的诊断统计（纸色 / 墨色 / 跨度 / 定位线档位与自动选择说明 / 位置门"
            "抠掉多少像素 / 暖色抑制抠掉多少像素 / 可见占比 / warnings），"
            "由 services/cutout.py 的 cutout() 返回；失败时为空字典"
        ),
    )
    width: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="产物宽度（像素）"
    )
    height: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="产物高度（像素）"
    )
    size_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="产物文件大小（字节）"
    )
    elapsed_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="该张耗时（秒）"
    )
    error_message: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="失败原因"
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="开始时间（UTC）"
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="结束时间（UTC）"
    )

    job: Mapped["BackgroundJob"] = relationship(back_populates="items")

    __table_args__ = (
        Index("ix_background_job_items_job", "job_id"),
        Index("ix_background_job_items_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<BackgroundJobItem id={self.id} job={self.job_id} name={self.source_name}>"
