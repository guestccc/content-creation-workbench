"""素材抓取（MediaCrawler）任务 ORM 模型。

形态与 SubtitleJob 同族（**这张表本身就是队列**）：一个后台工作线程按
status='pending' 的行依次执行，取消、重启恢复、worker 崩溃三件事都不需要
额外的同步代码。状态常量在这里另立一份，而不是 import SubtitleJobStatus
—— 两个功能各有一套状态机，跨功能引用会把它们的演进焊死在一起。

与字幕提取的关键差别：**一个任务 = 一个 MediaCrawler 子进程**。关键词搜索
的多个关键词、详情模式的多个链接都在一次 CLI 调用里传给 MC，所以没有
items 子表 —— 单表即队列，也没有 partial 状态（单进程语义下没有
「部分成功」的任务级状态，个别笔记抓不到属正常爬取损耗，看结果数量即可）。

爬虫没有机器可读的进度输出（MC 的日志是给人看的），进度 = 已产出的
jsonl 行数，分母是创建任务时的**估算值**（见 service 的 expected_count 注释）。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.content import utcnow
from app.models.job_common import JobRemarkMixin


class CrawlJobStatus:
    """素材抓取任务状态常量。"""

    PENDING = "pending"  # 排队中，等待工作线程认领
    RUNNING = "running"  # 已认领，MC 子进程在跑
    SUCCESS = "success"  # 结束且抓到了内容（个别笔记失败不影响，以产物为准）
    FAILED = "failed"  # 结束且没有抓到任何内容
    CANCELLED = "cancelled"  # 已取消（已抓到的内容保留在输出目录）

    ALL: tuple = (PENDING, RUNNING, SUCCESS, FAILED, CANCELLED)

    # 终态：不允许再发生状态流转
    TERMINAL: tuple = (SUCCESS, FAILED, CANCELLED)


class CrawlPlatform:
    """支持的平台常量（值与 MediaCrawler CLI 的 --platform 参数一致）。"""

    XHS = "xhs"  # 小红书
    DY = "dy"  # 抖音
    KS = "ks"  # 快手
    BILI = "bili"  # B 站
    WB = "wb"  # 微博
    TIEBA = "tieba"  # 百度贴吧
    ZHIHU = "zhihu"  # 知乎

    ALL: tuple = (XHS, DY, KS, BILI, WB, TIEBA, ZHIHU)

    # 中文名。放这里而不是前端写死：环境自检的登录态列表也要用它，
    # 两边各写一份迟早对不上。
    LABELS: dict = {
        XHS: "小红书",
        DY: "抖音",
        KS: "快手",
        BILI: "B站",
        WB: "微博",
        TIEBA: "贴吧",
        ZHIHU: "知乎",
    }


class CrawlerType:
    """抓取模式常量（值与 MC 的 --type 参数一致）。"""

    SEARCH = "search"  # 关键词搜索
    DETAIL = "detail"  # 指定笔记/作品详情
    CREATOR = "creator"  # 创作者主页

    ALL: tuple = (SEARCH, DETAIL, CREATOR)


class CrawlLoginType:
    """登录方式常量（值与 MC 的 --lt 参数一致）。

    phone（短信验证码）不在清单里：它依赖一套外部 Redis 短信转发服务
    （recv_sms.py），页面上暴露一个必然用不了的选项只会误导。
    """

    QRCODE = "qrcode"  # 扫码登录（首次会弹 Chrome 窗口）
    COOKIE = "cookie"  # 直接注入 cookie 串，无人值守

    ALL: tuple = (QRCODE, COOKIE)


class CrawlJob(Base, JobRemarkMixin):
    """素材抓取任务表。"""

    __tablename__ = "crawl_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="主键")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CrawlJobStatus.PENDING, comment="任务状态"
    )
    platform: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="平台：xhs/dy/ks/bili/wb/tieba/zhihu"
    )
    crawler_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="抓取模式：search/detail/creator"
    )
    login_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="登录方式：qrcode/cookie"
    )
    params: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        comment=(
            "抓取参数（白名单键：keywords/ids/creators/start_page/max_notes/"
            "get_comments/get_sub_comments/max_comments/headless/max_concurrency，"
            "见 services/crawl_runner.py）"
        ),
    )
    # cookie 是凭据：只入库供 runner 拼 argv，绝不进任何响应 schema。
    login_cookies: Mapped[str] = mapped_column(
        Text, nullable=False, default="", comment="cookie 登录串（敏感，不回显）"
    )

    # ---------- 派生关系：补抓任务指回它的来源 ----------
    # 形态照抄 BackgroundJob 的派生口径（models/background_job.py）：普通抓取任务
    # 两列都是空，只有「对某条笔记补抓评论」这类派生任务才有值。
    #
    # 存在的意义是**把派生任务从历史列表里摘出去**（list_jobs 按
    # source_crawl_job_id IS NULL 过滤）—— 补抓是用户在评论弹窗里的一个动作，
    # 不是一次独立的抓取，混进历史列表只会把真正想要的那些任务淹掉。
    # 另外删除原任务时要靠它级联删掉派生任务。
    source_crawl_job_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="派生来源的任务 id；普通任务为 NULL"
    )
    # 非空 + 默认空串（与 BackgroundJob.source_background_note_id 一致）：
    # 少一个 None/"" 的分支，筛选条件写起来只有一种形状。
    source_crawl_note_id: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", comment="派生任务针对的笔记 id；普通任务为空串"
    )

    # ---------- 产物位置：一个任务一个独立输出目录 ----------
    # nullable 的原因：目录名带任务 id（job_<id>），而 id 要第一次 flush 才有
    # —— INSERT 那一刻这两列还是 NULL，同一事务内紧接着补上，提交时不会缺。
    output_dir: Mapped[Optional[str]] = mapped_column(
        String(1000),
        nullable=True,
        comment="任务输出目录（materials/crawl/job_<id>/，创建时 flush 出 id 后定）",
    )
    log_path: Mapped[Optional[str]] = mapped_column(
        String(1000), nullable=True, comment="MC 子进程日志文件（失败时读尾部展示）"
    )

    # ---------- 进度字段：轮询接口直接读这些列，不重新计算 ----------
    # expected 是估算值：平台会抬升最小每页数、结果有去重与风控截流，
    # 实际值完全可能少于它。进度条封顶 99% 到终态，不承诺精确百分比。
    expected_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="预估抓取总量（创建时估算）"
    )
    crawled_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已抓到的笔记数（扫 jsonl 行数，节流更新）"
    )
    note_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="终态定稿的笔记数（重扫 jsonl 得到）"
    )
    elapsed_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="已用时（节流更新）。爬虫没有可解析的进度输出，已用时与已抓条数是唯二如实的实时数字",
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
        Text, nullable=False, default="", comment="失败原因或收尾备注（如「退出码非 0 但已有产物」）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow, comment="更新时间（UTC）"
    )

    __table_args__ = (
        Index("ix_crawl_jobs_status", "status"),
        # 工作线程按「状态 + id」扫待执行任务，建立联合索引
        Index("ix_crawl_jobs_status_id", "status", "id"),
        Index("ix_crawl_jobs_created", "created_at"),
        Index("ix_crawl_jobs_platform", "platform"),
        # 派生任务：按「来源任务 + 笔记」查已有补抓，以及删原任务时找级联对象
        Index("ix_crawl_jobs_source", "source_crawl_job_id", "source_crawl_note_id"),
    )

    def __repr__(self) -> str:
        return f"<CrawlJob id={self.id} platform={self.platform} status={self.status}>"
