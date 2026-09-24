"""素材抓取（MediaCrawler）相关的 Pydantic 模型。

包含四组：
1. 环境自检 —— MediaCrawler 装没装、解释器、Node、登录态缓存、媒体开关；
2. 任务创建 —— 平台 / 模式 / 登录方式与各模式的专属参数；
3. 任务响应 —— 进度字段（已抓条数 / 预估总量 / 已用时）；
4. 抓取结果 —— 跨平台归一化后的笔记列表。

安全约定：`login_cookies` 是凭据，只存在于请求模型与数据库列里，
**任何响应模型都不包含它**（CrawlJobResponse.from_model 显式不取）。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer, field_validator

from app.models.crawl_ai_copy import CrawlNoteAiCopy
from app.models.crawl_job import (
    CrawlJob,
    CrawlJobStatus,
    CrawlLoginType,
    CrawlPlatform,
    CrawlerType,
)
from app.schemas.common import TimestampMixin, to_utc_iso


class InstallHint(BaseModel):
    """一条安装指引。命令只是文本，供前端展示与复制。"""

    title: str = Field(description="这一步做什么")
    command: str = Field(default="", description="要执行的命令；为空表示这一步没有命令")
    note: str = Field(default="", description="补充说明")
    url: str = Field(default="", description="相关链接")


class PlatformLoginState(BaseModel):
    """一个平台在本机的登录态缓存情况。"""

    platform: str = Field(description="平台标识（xhs/dy/...）")
    platform_label: str = Field(description="平台中文名")
    cdp: bool = Field(description="是否有 CDP 模式的登录态目录")
    standard: bool = Field(description="是否有标准模式的登录态目录")
    updated_at: str = Field(default="", description="登录态目录的最后修改时间（ISO）")


class CrawlEnvironmentResponse(BaseModel):
    """素材抓取功能的运行环境自检结果。"""

    installed: bool = Field(description="是否探测到可用的 MediaCrawler")
    ready: bool = Field(description="能否开始抓取：装了 MC 且能拼出可执行的调用前缀")
    launcher: List[str] = Field(description="后端实际调用的命令前缀（诊断用）")
    kind: str = Field(description="命中方式：venv-python / uv-run；未安装时为空")
    mc_root: str = Field(description="探测时使用的 MediaCrawler 根目录")
    python_version: str = Field(description="MC 目标解释器的 Python 版本，拿不到为空")
    detail: str = Field(description="未安装时的原因说明")
    # Node 只在抖音 / 知乎的签名环节需要（pyexecjs 跑 js），其它平台无所谓
    node_version: str = Field(default="", description="Node.js 版本；为空表示没找到")
    node_required_platforms: List[str] = Field(
        default_factory=lambda: [CrawlPlatform.DY, CrawlPlatform.ZHIHU],
        description="哪些平台必须有 Node.js（它们的签名依赖 pyexecjs）",
    )
    login_states: List[PlatformLoginState] = Field(
        default_factory=list, description="本机已有登录态缓存的平台（扫码一次后不用再扫）"
    )
    media_enabled: bool = Field(
        default=False, description="MC 是否开启了媒体文件下载（ENABLE_GET_MEIDAS）"
    )
    zhihu_creator_cli_supported: bool = Field(
        default=False,
        description="MC 的 --creator_id 是否已支持知乎（原版缺该分支，需打补丁）",
    )
    default_output_dir: str = Field(description="默认输出目录（materials/crawl）")
    install_hints: List[InstallHint] = Field(description="未就绪时的分步安装指引")
    warnings: List[str] = Field(default_factory=list, description="需要提醒的配置问题")


class CrawlJobCreate(BaseModel):
    """创建素材抓取任务。"""

    platform: str = Field(..., description=f"平台：{'/'.join(CrawlPlatform.ALL)}")
    crawler_type: str = Field(..., description=f"抓取模式：{'/'.join(CrawlerType.ALL)}")
    login_type: str = Field(..., description=f"登录方式：{'/'.join(CrawlLoginType.ALL)}")
    keywords: Optional[List[str]] = Field(
        default=None, description="search 模式的关键词列表（每项一个词，必填）"
    )
    ids: Optional[List[str]] = Field(
        default=None, description="detail 模式的笔记/作品链接或 ID 列表（每项一条，必填）"
    )
    creators: Optional[List[str]] = Field(
        default=None, description="creator 模式的创作者主页链接或 ID 列表（每项一条，必填）"
    )
    cookies: str = Field(default="", max_length=8000, description="cookie 登录串（login_type=cookie 时必填）")
    start_page: int = Field(default=1, ge=1, le=100, description="search 模式的起始页码")
    max_notes: int = Field(default=20, ge=1, description="最大抓取条数（各模式共用）")
    get_comments: bool = Field(default=True, description="是否抓评论")
    get_sub_comments: bool = Field(default=False, description="是否抓二级评论")
    max_comments: int = Field(default=10, ge=0, le=200, description="每条笔记最多抓多少条一级评论")
    headless: bool = Field(default=False, description="是否无头跑浏览器（扫码的二维码走系统看图软件，与窗口无关；遇滑块验证请关闭重试）")
    max_concurrency: int = Field(default=1, ge=1, description="并发抓取数（后端有上限钳制）")

    @field_validator("platform")
    @classmethod
    def _check_platform(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in CrawlPlatform.ALL:
            raise ValueError(f"不支持的平台：{value}（可选：{', '.join(CrawlPlatform.ALL)}）")
        return cleaned

    @field_validator("crawler_type")
    @classmethod
    def _check_crawler_type(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in CrawlerType.ALL:
            raise ValueError(f"不支持的抓取模式：{value}（可选：{', '.join(CrawlerType.ALL)}）")
        return cleaned

    @field_validator("login_type")
    @classmethod
    def _check_login_type(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in CrawlLoginType.ALL:
            raise ValueError(f"不支持的登录方式：{value}（可选：{', '.join(CrawlLoginType.ALL)}）")
        return cleaned

    @staticmethod
    def _clean_list(value: Optional[List[str]]) -> List[str]:
        """列表参数统一清洗：去空白项、去首尾空格、去重保序。"""
        if not value:
            return []
        result: List[str] = []
        seen = set()
        for item in value:
            cleaned = item.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            result.append(cleaned)
        return result

    @field_validator("keywords", "ids", "creators")
    @classmethod
    def _check_lists(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        cleaned = cls._clean_list(value)
        return cleaned or None


class CrawlJobResponse(TimestampMixin):
    """素材抓取任务详情。"""

    id: int = Field(description="主键")
    status: str = Field(description="任务状态")
    platform: str = Field(description="平台")
    platform_label: str = Field(description="平台中文名")
    crawler_type: str = Field(description="抓取模式")
    login_type: str = Field(description="登录方式")
    params: dict = Field(description="实际生效的抓取参数（不含 cookies）")
    output_dir: str = Field(description="任务输出目录")
    expected_count: int = Field(description="预估抓取总量（估算值，见 model 注释）")
    crawled_count: int = Field(description="已抓到的笔记数")
    note_count: int = Field(description="终态定稿的笔记数")
    elapsed_seconds: int = Field(description="已用时（秒）")
    progress_percent: int = Field(description="进度百分比（服务端算好，running 时封顶 99）")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")
    error_message: str = Field(description="失败原因或收尾备注")
    remark: str = Field(description="备注（用户可编辑，最多 200 字；空串表示没写）")
    phase: str = Field(
        default="",
        description="running 任务的当前阶段（starting/login_cookie/login_scan/login_redirect/crawling/finishing），"
        "非 running 时为空串",
    )
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    @field_serializer("started_at", "finished_at")
    def _serialize_optional_times(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value) if value is not None else None

    @classmethod
    def from_model(cls, model: CrawlJob) -> "CrawlJobResponse":
        """由 ORM 对象构造响应模型。**不取 login_cookies**（凭据不回显）。"""
        return cls(
            id=model.id,
            status=model.status,
            platform=model.platform,
            platform_label=CrawlPlatform.LABELS.get(model.platform, model.platform),
            crawler_type=model.crawler_type,
            login_type=model.login_type,
            params=dict(model.params or {}),
            output_dir=model.output_dir,
            expected_count=model.expected_count,
            crawled_count=model.crawled_count,
            note_count=model.note_count,
            elapsed_seconds=model.elapsed_seconds,
            progress_percent=_progress_percent(model),
            started_at=model.started_at,
            finished_at=model.finished_at,
            error_message=model.error_message,
            remark=model.remark,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


def _progress_percent(model: CrawlJob) -> int:
    """按已抓条数 / 预估总量算进度。

    分母是估算值（平台有最小每页数、结果有去重与风控截流），所以 running
    期间封顶 99% —— 100% 只留给终态，避免「进度条满了任务还在跑」或
    「满了又缩回去」的观感。
    """
    if model.status == CrawlJobStatus.SUCCESS:
        return 100
    if not model.expected_count:
        return 0
    percent = int(round(100 * model.crawled_count / model.expected_count))
    return max(0, min(99, percent))


class CrawlJobListData(BaseModel):
    """任务列表数据。"""

    total: int = Field(description="满足条件的总条数")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页条数")
    items: List[CrawlJobResponse] = Field(description="当前页数据")


class CrawlNoteResponse(BaseModel):
    """一条归一化后的笔记/作品（跨平台字段已对齐）。"""

    index: int = Field(description="序号，从 1 开始")
    id: str = Field(description="笔记/作品 ID（各平台原生 ID）")
    type: str = Field(default="", description="内容类型（笔记/视频/回答等，平台原生值）")
    title: str = Field(default="", description="标题；微博等无标题平台为空，前端用摘要兜底")
    desc: str = Field(default="", description="正文/摘要")
    nickname: str = Field(default="", description="作者昵称（MC 已脱敏）")
    liked_count: str = Field(default="", description="点赞数（MC 落盘为字符串，原样透传）")
    collected_count: str = Field(
        default="",
        description="收藏数（仅 xhs/dy 的 collected_count 与 B 站的 favorite，其余平台为空）",
    )
    comment_count: str = Field(default="", description="评论数")
    share_count: str = Field(default="", description="分享/转发数")
    publish_time: str = Field(default="", description="发布时间（统一转 ISO，转不了原样返回）")
    url: str = Field(default="", description="原文链接")
    cover: str = Field(default="", description="封面图 URL（远程）")
    images: List[str] = Field(default_factory=list, description="图片 URL 列表（来自 jsonl）")
    local_images: List[str] = Field(
        default_factory=list,
        description="已下载到本地的图片相对路径（相对任务输出目录，走 /media 接口取）",
    )
    local_videos: List[str] = Field(default_factory=list, description="已下载到本地的视频相对路径")
    local_image_dir: str = Field(
        default="",
        description=(
            "本地图片所在目录的绝对路径（供下游「一键换背景」把这一批图直接带过去）；"
            "没有本地图片时为空串"
        ),
    )
    source_keyword: str = Field(default="", description="来源关键词（search 模式）")


class CrawlResultsData(BaseModel):
    """任务结果（归一化笔记列表）。"""

    total: int = Field(description="笔记总数")
    notes: List[CrawlNoteResponse] = Field(description="归一化后的笔记列表")


class CrawlLogData(BaseModel):
    """MC 子进程日志尾部。"""

    log: str = Field(description="日志尾部文本（默认 4000 字符）")


# ----------------------------------------------------------------------
# 笔记 AI 文案（一条（任务, 笔记）只留最新一份，「换一批」是覆盖）
# ----------------------------------------------------------------------


class NotePlatformCopy(BaseModel):
    """一个平台的 AI 生成文案。"""

    titles: List[str] = Field(description="候选标题（至多 5 条）")
    intros: List[str] = Field(description="候选简介（至多 3 条）")


class CrawlNoteAiCopyPlatforms(BaseModel):
    """两个目标平台各一份。"""

    xhs: NotePlatformCopy = Field(description="小红书文案")
    dy: NotePlatformCopy = Field(description="抖音文案")


class CrawlNoteAiCopyResponse(TimestampMixin):
    """一条笔记已落库的 AI 文案。"""

    job_id: int = Field(description="来源抓取任务 id")
    note_id: str = Field(description="平台原生笔记 id")
    platforms: CrawlNoteAiCopyPlatforms = Field(description="两个平台的候选文案")
    model: str = Field(description="生成用的模型名")
    tokens_used: int = Field(description="本次生成的 token 用量")
    reasoning: str = Field(
        default="", description="模型思维链原文；老行或模型不支持思考时是空串"
    )
    created_at: datetime = Field(description="首次生成时间")
    updated_at: datetime = Field(description="最近一次生成（换一批）时间")

    @classmethod
    def from_model(cls, model: CrawlNoteAiCopy) -> "CrawlNoteAiCopyResponse":
        """由 ORM 对象构造响应模型；payload dict 的形状由 parse_copy_payload 保证。"""
        return cls(
            job_id=model.crawl_job_id,
            note_id=model.note_id,
            platforms=CrawlNoteAiCopyPlatforms(**(model.payload or {})),
            model=model.model,
            tokens_used=model.tokens_used,
            # 老行没有思维链（补列补出来的空串），前端据此整块不渲染
            reasoning=model.reasoning or "",
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class CrawlNoteAiCopyData(BaseModel):
    """「这条笔记有没有已生成的 AI 文案」的查询结果。"""

    found: bool = Field(description="是否已有落库的生成结果")
    result: Optional[CrawlNoteAiCopyResponse] = Field(
        default=None, description="已落库的文案；found=false 时为 null"
    )


class CrawlNoteAiCopyGenerate(BaseModel):
    """生成/换一批 AI 文案的请求体。note_id 走 body 不走 path（平台原生 id
    字符集未验证过，可能含 URL 敏感字符）。"""

    note_id: str = Field(min_length=1, max_length=200, description="平台原生笔记 id")


# ----------------------------------------------------------------------
# 笔记评论（查看 + 对单条笔记补抓）
# ----------------------------------------------------------------------


class CrawlComment(BaseModel):
    """一条归一化后的评论（跨平台字段已对齐，见 services/crawl_comments.py）。

    children 是子评论（二级及更深，深度封顶后提升上来的也算一级）。
    orphan=true 表示它的父评论没被抓到（只抓到部分子评论时会出现），
    前端要标明「父评论未抓到」，不能让它看起来像一级评论。
    """

    id: str = Field(description="评论 id（平台原生值；缺失时为回退键，见 crawl_comments）")
    content: str = Field(default="", description="评论内容")
    nickname: str = Field(default="", description="评论者昵称")
    liked_count: str = Field(
        default="",
        description="点赞数（原样透传字符串；快手/贴吧不落盘点赞，给空串以区分「0 个赞」）",
    )
    created_at: str = Field(default="", description="发布时间（统一转 ISO，转不了原样返回）")
    sub_comment_count: int = Field(
        default=0,
        description="平台报告的子评论总数（可能比 children 多 —— 多出来的是没抓到的）",
    )
    pictures: List[str] = Field(
        default_factory=list,
        description=(
            "图片地址列表（仅 xhs/dy 有）。**两种形态混合**：http(s) 是平台"
            "原图 URL（时效签名，过期即 403 —— 通常是没缓存成功的）；其余是"
            "相对任务输出目录的本地缓存路径，走 /jobs/{id}/media/{path} 取"
        ),
    )
    orphan: bool = Field(default=False, description="父评论未抓到、被提升为一级展示")
    children: List["CrawlComment"] = Field(default_factory=list, description="子评论")


class CrawlCommentsConfig(BaseModel):
    """原任务的评论采集配置（弹窗三态判定的依据）。"""

    enabled: bool = Field(
        description="原任务是否实际会抓评论（get_comments 且 max_comments>0 才算）"
    )
    max_comments: int = Field(description="原任务每条笔记的一级评论上限")
    sub_comments: bool = Field(description="原任务是否抓二级评论")


class CrawlCommentRefetchState(BaseModel):
    """一次评论补抓任务的状态（补抓任务不进历史列表，这是它唯一的可见入口）。"""

    job_id: int = Field(description="补抓任务 id（取消用 /jobs/{id}/cancel）")
    status: str = Field(description="任务状态（pending/running/success/failed/cancelled）")
    error_message: str = Field(default="", description="失败原因")
    comment_count: int = Field(
        default=0,
        description="这次补抓自己抓到的评论条数（不能用任务的 note_count 顶，那边数的是内容行）",
    )
    queued_ahead: int = Field(
        default=0,
        description="排在它前面的待执行任务数（MC 单任务串行，pending 时解释「为什么在转圈」）",
    )


class NoteCommentsData(BaseModel):
    """一条笔记的评论查询结果：评论树 + 原任务配置 + 最新一次补抓状态。"""

    job_id: int = Field(description="根任务 id（传进来的是派生任务时已解析回根任务）")
    note_id: str = Field(description="平台原生笔记 id")
    platform: str = Field(description="平台标识")
    comments: List[CrawlComment] = Field(description="评论树（一级评论，子评论在 children）")
    total: int = Field(description="评论总条数（含所有层级）")
    top_level_total: int = Field(description="一级评论条数")
    comments_config: CrawlCommentsConfig = Field(description="原任务的评论采集配置")
    login_type: str = Field(description="根任务的登录方式（补抓设置里登录方式的默认选中项）")
    headless: bool = Field(description="根任务是否无头跑浏览器（补抓设置里无头开关的默认值）")
    refetch: Optional[CrawlCommentRefetchState] = Field(
        default=None, description="最新一次补抓任务的状态；从没补抓过为 null"
    )


class CrawlCommentRefetchCreate(BaseModel):
    """对一条笔记补抓评论的请求体。

    note_id 走 body 不走 path（与 AI 文案同口径：平台原生 id 字符集未验证过，
    可能含 `/` 之类的字符被 path 参数吞掉）。

    条数与二级开关**不继承原任务**（触发补抓的典型场景恰恰是原任务没开或
    max_comments=0，继承它等于抓 0 条还报 success），由用户在弹窗里现选。

    登录方式 / 无头是**可选覆盖**：不传 = 沿用原任务（cookie 登录的常见败因是
    串已过期，补抓是用户换登录方式的天然时机，所以弹窗里要能改选）。
    cookies 是凭据：只进不出，沿用原任务存的串由服务端读库完成，前端拿不到
    也不需要拿到。
    """

    note_id: str = Field(min_length=1, max_length=200, description="平台原生笔记 id")
    max_comments: int = Field(
        default=20, ge=1, le=200, description="补抓的一级评论条数上限"
    )
    sub_comments: bool = Field(default=True, description="是否补抓二级评论")
    login_type: Optional[str] = Field(
        default=None,
        description=f"登录方式：{'/'.join(CrawlLoginType.ALL)}；不传 = 沿用原任务",
    )
    cookies: Optional[str] = Field(
        default=None,
        max_length=8000,
        description="新贴的 cookie 串；login_type=cookie 且不传（或留空）时沿用原任务存的 cookie",
    )
    headless: Optional[bool] = Field(
        default=None, description="是否无头跑浏览器；不传 = 沿用原任务"
    )

    @field_validator("login_type")
    @classmethod
    def _check_login_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        cleaned = value.strip().lower()
        if cleaned not in CrawlLoginType.ALL:
            raise ValueError(f"不支持的登录方式：{value}（可选：{', '.join(CrawlLoginType.ALL)}）")
        return cleaned
