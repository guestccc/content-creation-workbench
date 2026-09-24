"""素材抓取（MediaCrawler）接口。

路由注册顺序说明：/environment 等静态路径必须写在 /{job_id} 之前，
否则会被路径参数吞掉返回 422。

接口一览：
- GET    /environment             运行环境自检（MC / 解释器 / Node / 登录态）
- POST   /jobs                    创建抓取任务
- GET    /jobs                    历史任务分页列表
- GET    /jobs/{id}               任务详情（轮询进度也用它）
- GET    /jobs/{id}/results       归一化笔记列表（跨平台字段已对齐）
- GET    /jobs/{id}/ai-copies     某条笔记已生成的 AI 文案（?note_id=）
- POST   /jobs/{id}/ai-copies/stream  流式生成/换一批某条笔记的 AI 文案（SSE）
- GET    /jobs/{id}/comments      某条笔记的评论树 + 补抓状态（?note_id=）
- POST   /jobs/{id}/comments/refetch  对一条笔记补抓评论（建派生任务，不进历史列表）
- GET    /jobs/{id}/log           MC 子进程日志尾部
- GET    /jobs/{id}/media/{path}  已下载的本地媒体文件（图片/视频）
- POST   /jobs/{id}/cancel        取消任务
- POST   /jobs/{id}/retry         重试（按原参数新建一条任务，返回的就是新任务）
- PUT    /jobs/{id}/remark       更新任务备注（空串 = 清空）
- POST   /jobs/batch-delete       批量删除任务记录（payload 带 purge_files 时产物一并清）
- DELETE /jobs/{id}               删除任务记录（?purge_files=true 时产物一并清）
"""

import json
from typing import Iterator, Tuple

from fastapi import APIRouter, Path as PathParam, Query
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import (
    CrawlJobServiceDep,
    CrawlNoteCopyServiceDep,
    SessionFactoryDep,
)
from app.core.logging import get_logger
from app.models.crawl_job import CrawlJobStatus, CrawlPlatform
from app.schemas.common import ApiResponse, JobBatchDeleteRequest, JobRemarkUpdate
from app.schemas.crawl_job import (
    CrawlCommentRefetchCreate,
    CrawlEnvironmentResponse,
    CrawlJobCreate,
    CrawlJobListData,
    CrawlJobResponse,
    CrawlLogData,
    CrawlNoteAiCopyData,
    CrawlNoteAiCopyGenerate,
    CrawlNoteAiCopyResponse,
    CrawlNoteResponse,
    CrawlResultsData,
    NoteCommentsData,
)
from app.services.ai_client import AiError
from app.services.crawler_env import probe_environment

router = APIRouter(prefix="/crawl", tags=["素材抓取"])
logger = get_logger(__name__)


def _ai_error_code(kind: str) -> str:
    """把失败的种类映射成对外的错误码（code 给前端分支用，message 直接展示）。

    前端只认 AI_NOT_CONFIGURED / AI_AUTH_FAILED 给「去配置」入口（key 填错
    也该去配置），其余一律给「重试」。

    bad_response 与解析层的 ValueError 归同一个码（AI_BAD_RESPONSE）：对用户
    都是「模型这轮返回的内容没法用」，重试是唯一动作 —— 分成两个码只会让人
    以为要分别处理。

    kind 是 service 层给的：`AiError.kind` 加一个 `"db"`（写库失败）。
    认不出的（含 service 兜底的 "unknown"）一律 AI_UPSTREAM。
    """
    mapping = {
        "config": "AI_NOT_CONFIGURED",
        "auth": "AI_AUTH_FAILED",
        "rate_limit": "AI_RATE_LIMIT",
        "timeout": "AI_TIMEOUT",
        "bad_response": "AI_BAD_RESPONSE",
        "db": "DB_ERROR",
    }
    return mapping.get(kind, "AI_UPSTREAM")


def _sse_frames(events: Iterator[Tuple[str, dict]]) -> Iterator[str]:
    """把 service 的 (事件名, 数据) 拼成 SSE 帧。

    帧格式固定 `event: X\\ndata: {...}\\n\\n`（双换行 = 一帧结束）。数据统一走
    JSON 且 `ensure_ascii=False`：中文不转义，省带宽，抓包也看得懂。

    两处转换放在这里而不是 service 里，是为了让 service 不认识 HTTP 那一套 ——
    service 只说「失败的种类是 auth」，错误码由本层翻译。
    """
    for event, data in events:
        if event == "error":
            payload = {
                "code": _ai_error_code(str(data.get("kind", ""))),
                "message": str(data.get("message", "")),
            }
        elif event == "done":
            payload = {
                "result": CrawlNoteAiCopyResponse.from_model(data["row"]).model_dump(
                    mode="json"
                )
            }
        else:
            payload = data
        yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get(
    "/environment",
    response_model=ApiResponse[CrawlEnvironmentResponse],
    summary="素材抓取运行环境自检",
)
def get_environment(
    refresh: bool = Query(default=False, description="绕过探测缓存重新检测"),
) -> ApiResponse[CrawlEnvironmentResponse]:
    """探测 MediaCrawler / 解释器 / Node / 登录态缓存，未就绪时给分步安装指引。

    指引只是文本，后端绝不替用户执行安装 —— 网页触发的安装命令既不可靠
    （权限、网络、杀毒软件），也不该有（网页进程不该有装软件的权力）。
    """
    return ApiResponse(data=CrawlEnvironmentResponse(**probe_environment(refresh=refresh)))


@router.post(
    "/jobs",
    response_model=ApiResponse[CrawlJobResponse],
    status_code=201,
    summary="创建素材抓取任务",
)
def create_job(
    payload: CrawlJobCreate,
    service: CrawlJobServiceDep,
) -> ApiResponse[CrawlJobResponse]:
    """创建任务并排入队列，真正的抓取由后台工作线程执行。

    注意：扫码登录的任务会弹出真实 Chrome 窗口等待扫码（首次登录），
    登录态缓存后同类任务不再弹窗 —— 这是预期行为。
    """
    job = service.create_job(payload)
    return ApiResponse(data=CrawlJobResponse.from_model(job))


@router.get(
    "/jobs",
    response_model=ApiResponse[CrawlJobListData],
    summary="分页查询素材抓取任务",
)
def list_jobs(
    service: CrawlJobServiceDep,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=10, ge=1, le=100, description="每页条数，最大 100"),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description=f"按状态过滤：{'/'.join(CrawlJobStatus.ALL)}",
    ),
    platform: str | None = Query(
        default=None,
        description=f"按平台过滤：{'/'.join(CrawlPlatform.ALL)}",
    ),
) -> ApiResponse[CrawlJobListData]:
    """分页查询历史任务。"""
    items, total = service.list_jobs(
        page=page, page_size=page_size, status=status_filter, platform=platform
    )
    return ApiResponse(
        data=CrawlJobListData(
            total=total,
            page=page,
            page_size=page_size,
            items=[CrawlJobResponse.from_model(item) for item in items],
        )
    )


# 静态路径 /jobs/batch-delete 必须写在 /jobs/{job_id} 之前（见文件头说明）
@router.post(
    "/jobs/batch-delete",
    response_model=ApiResponse[dict],
    summary="批量删除素材抓取任务记录",
)
def batch_delete_jobs(
    payload: JobBatchDeleteRequest,
    service: CrawlJobServiceDep,
) -> ApiResponse[dict]:
    """整批删除（全成功或全失败）；purge_files=true 时输出目录一并清掉。"""
    ids = service.delete_jobs(payload.ids, purge_files=payload.purge_files)
    return ApiResponse(data={"ids": ids, "count": len(ids)})


@router.get(
    "/jobs/{job_id}",
    response_model=ApiResponse[CrawlJobResponse],
    summary="获取素材抓取任务详情",
)
def get_job(
    service: CrawlJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[CrawlJobResponse]:
    """按 ID 获取任务详情，前端轮询进度也调它。"""
    job = service.get_job(job_id)
    data = CrawlJobResponse.from_model(job)
    data.phase = service.get_job_phase(job)
    return ApiResponse(data=data)


@router.get(
    "/jobs/{job_id}/results",
    response_model=ApiResponse[CrawlResultsData],
    summary="任务的归一化笔记列表",
)
def get_results(
    service: CrawlJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[CrawlResultsData]:
    """读取任务产物：跨平台字段已归一化（标题/作者/点赞/链接/图片）。

    终态后是定稿；running 中调用返回的是当前已抓到的部分（数据来自
    jsonl 落盘行，天然只含已完成的条目）。
    """
    notes = service.get_results(job_id)
    return ApiResponse(
        data=CrawlResultsData(
            total=len(notes),
            notes=[
                CrawlNoteResponse(index=index, **note)
                for index, note in enumerate(notes, start=1)
            ],
        )
    )


@router.get(
    "/jobs/{job_id}/ai-copies",
    response_model=ApiResponse[CrawlNoteAiCopyData],
    summary="某条笔记已生成的 AI 文案",
)
def get_note_ai_copy(
    service: CrawlNoteCopyServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    note_id: str = Query(..., min_length=1, max_length=200, description="平台原生笔记 id"),
) -> ApiResponse[CrawlNoteAiCopyData]:
    """读一条笔记已落库的 AI 文案；没生成过返回 found=false（不是错误）。

    note_id 走 query 不走 path：平台原生 id 的字符集没验证过，含 `/` 之类
    的字符会被 path 参数吞掉。
    """
    copy = service.get_copy(job_id, note_id)
    return ApiResponse(
        data=CrawlNoteAiCopyData(
            found=copy is not None,
            result=CrawlNoteAiCopyResponse.from_model(copy) if copy is not None else None,
        )
    )


@router.post(
    "/jobs/{job_id}/ai-copies/stream",
    summary="流式生成/换一批某条笔记的 AI 文案（SSE）",
)
def stream_note_ai_copy(
    payload: CrawlNoteAiCopyGenerate,
    service: CrawlNoteCopyServiceDep,
    session_factory: SessionFactoryDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> StreamingResponse:
    """流式生成，边生成边把思维链推给前端（前端据此逐字显示 AI 的思考过程）。

    一条（任务, 笔记）只留最新一份：已生成过再调就是「换一批」（覆盖）。
    AI 调用/解析失败时不落行 —— 库里有内容 = 有一份能用的文案。

    帧契约（`event` / `data`）：

    | event       | data                                        | 含义           |
    | ----------- | ------------------------------------------- | -------------- |
    | `reasoning` | `{"text": "增量"}`                          | 思维链增量     |
    | `done`      | `{"result": CrawlNoteAiCopyResponse}`       | 解析+落库完成  |
    | `error`     | `{"code": "AI_...", "message": "..."}`      | 失败（含 code）|

    为什么「生成失败」也回 200：流一旦开出去就改不了状态码了。真正的 4xx/5xx
    只留给开流**之前**的预检（任务不存在 / 笔记不在结果里 → 404）。

    sync def + 同步生成器：Starlette 会把迭代丢进线程池，不阻塞事件循环。
    响应头带 `X-Accel-Buffering: no` 掐掉反向代理的缓冲（否则整段会攒到
    最后一次性到达，思维链的「逐字」就没了）。
    """
    ctx = service.prepare(job_id, payload.note_id)
    return StreamingResponse(
        _sse_frames(service.stream(ctx, session_factory)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/jobs/{job_id}/comments",
    response_model=ApiResponse[NoteCommentsData],
    summary="某条笔记的评论树（含补抓状态）",
)
def get_note_comments(
    service: CrawlJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    note_id: str = Query(..., min_length=1, max_length=200, description="平台原生笔记 id"),
) -> ApiResponse[NoteCommentsData]:
    """读一条笔记的评论：评论树 + 原任务的评论采集配置 + 最新一次补抓的状态。

    note_id 走 query 不走 path：平台原生 id 的字符集没验证过，含 `/` 之类
    的字符会被 path 参数吞掉（与 ai-copies 同口径）。

    读取口径是「根任务产物 + 它全部补抓任务的产物」合并去重：评论可能抓过
    好几次，散在各自输出目录里。前端在补抓进行中**轮询这个接口本身**即可
    （refetch 字段带补抓状态），不必再盯第二个数据源。

    三态判定交给前端，依据 comments_config.enabled：
    未开评论采集 → 引导补抓；开了但没抓到 → 「可能确实没有」；有评论 → 列表。
    """
    return ApiResponse(data=NoteCommentsData(**service.get_note_comments(job_id, note_id)))


@router.post(
    "/jobs/{job_id}/comments/refetch",
    response_model=ApiResponse[CrawlJobResponse],
    status_code=201,
    summary="对一条笔记补抓评论（建一条派生任务）",
)
def create_comment_refetch(
    payload: CrawlCommentRefetchCreate,
    service: CrawlJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[CrawlJobResponse]:
    """对一条笔记补抓评论：建一条 detail 模式的派生任务（只抓这一条）。

    派生任务**不进历史任务列表**（只在评论弹窗里露脸），删除原任务时会级联
    删除。登录方式 / 无头 / cookie **默认继承原任务、请求里给了就覆盖**
    （沿用旧 cookie 只能在服务端做 —— 凭据只存在于数据库行里）；但**不继承
    max_comments** —— 触发补抓的典型场景恰恰是原任务没开评论或条数为 0。

    这条笔记已经有一个未结束的补抓任务时返回 409（details 带那条任务的 id，
    前端拿它直接切到「正在补抓」，等价于幂等）。B 站、知乎 CDN 直链等补抓不了
    的情况在点击时直接 400 给出原因，不排一个注定失败的任务。
    """
    job = service.create_comment_refetch(
        job_id,
        payload.note_id,
        max_comments=payload.max_comments,
        sub_comments=payload.sub_comments,
        login_type=payload.login_type,
        cookies=payload.cookies,
        headless=payload.headless,
    )
    return ApiResponse(data=CrawlJobResponse.from_model(job))


@router.get(
    "/jobs/{job_id}/log",
    response_model=ApiResponse[CrawlLogData],
    summary="抓取任务日志尾部",
)
def get_log(
    service: CrawlJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    limit: int = Query(default=4000, ge=200, le=20000, description="返回的字符数"),
) -> ApiResponse[CrawlLogData]:
    """返回 MC 子进程日志的尾部（排查失败原因用，只读尾部不整读）。"""
    return ApiResponse(data=CrawlLogData(log=service.get_log_tail(job_id, limit=limit)))


@router.get(
    "/jobs/{job_id}/media/{media_path:path}",
    summary="任务的本地媒体文件",
)
def get_media(
    service: CrawlJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    media_path: str = PathParam(..., description="相对任务输出目录的文件路径"),
) -> FileResponse:
    """返回一个已下载的媒体文件（图片/视频）。

    安全说明：media_path 来自前端，服务层会把 resolve 后的路径限制在
    任务输出目录内（crawl_results.media_file），越界一律 404 ——
    放开校验这就是一个任意文件读取漏洞。
    """
    path = service.resolve_media_path(job_id, media_path)
    return FileResponse(path)


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=ApiResponse[CrawlJobResponse],
    summary="取消素材抓取任务",
)
def cancel_job(
    service: CrawlJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[CrawlJobResponse]:
    """取消排队中或执行中的任务，执行中的会整组杀掉 MC 子进程（含 CDP Chrome）。

    已抓到的 jsonl 与媒体保留在输出目录，结果接口照常可查。
    """
    job = service.cancel_job(job_id)
    return ApiResponse(data=CrawlJobResponse.from_model(job))


@router.post(
    "/jobs/{job_id}/retry",
    response_model=ApiResponse[CrawlJobResponse],
    status_code=201,
    summary="重试素材抓取任务（按原参数新建一条任务）",
)
def retry_job(
    service: CrawlJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[CrawlJobResponse]:
    """按旧任务的参数**新建一条任务**并返回它（新 id、新的输出目录）。

    抓取没有条目级状态（一条任务就是一个 MC 子进程），所以只能整任务重跑；
    而重跑必须是新建：输出目录按任务 id 定死，且 MC 的 jsonl 是追加语义、
    条数按行统计不去重 —— 就地重跑会把 note_count / crawled_count 算成两倍。

    cookie 登录的任务也能重试：cookie 只存在旧任务行里、任何响应都不带它，
    所以这一步只能在服务端做。

    返回 201 而不是 200：这确实创建了一条新资源，前端应当用返回的 id 去
    刷新历史列表，而不是把旧任务当成被更新的对象。
    """
    job = service.retry_job(job_id)
    return ApiResponse(data=CrawlJobResponse.from_model(job))


@router.put(
    "/jobs/{job_id}/remark",
    response_model=ApiResponse[CrawlJobResponse],
    summary="更新素材抓取任务备注",
)
def update_job_remark(
    payload: JobRemarkUpdate,
    service: CrawlJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[CrawlJobResponse]:
    """更新任务备注，空串表示清空。"""
    job = service.update_remark(job_id, payload)
    return ApiResponse(data=CrawlJobResponse.from_model(job))


@router.delete(
    "/jobs/{job_id}",
    response_model=ApiResponse[dict],
    summary="删除素材抓取任务记录",
)
def delete_job(
    service: CrawlJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    purge_files: bool = Query(default=False, description="是否连同磁盘上的任务产物一起删除"),
) -> ApiResponse[dict]:
    """删除任务记录；默认保留输出目录，purge_files=true 时连同 jsonl/媒体一起清掉。"""
    service.delete_job(job_id, purge_files=purge_files)
    return ApiResponse(data={"id": job_id})
