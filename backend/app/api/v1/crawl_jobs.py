"""素材抓取（MediaCrawler）接口。

路由注册顺序说明：/environment 等静态路径必须写在 /{job_id} 之前，
否则会被路径参数吞掉返回 422。

接口一览：
- GET    /environment             运行环境自检（MC / 解释器 / Node / 登录态）
- POST   /jobs                    创建抓取任务
- GET    /jobs                    历史任务分页列表
- GET    /jobs/{id}               任务详情（轮询进度也用它）
- GET    /jobs/{id}/results       归一化笔记列表（跨平台字段已对齐）
- GET    /jobs/{id}/log           MC 子进程日志尾部
- GET    /jobs/{id}/media/{path}  已下载的本地媒体文件（图片/视频）
- POST   /jobs/{id}/cancel        取消任务
- DELETE /jobs/{id}               删除任务记录
"""

from fastapi import APIRouter, Path as PathParam, Query
from fastapi.responses import FileResponse

from app.api.deps import CrawlJobServiceDep
from app.core.logging import get_logger
from app.models.crawl_job import CrawlJobStatus, CrawlPlatform
from app.schemas.common import ApiResponse
from app.schemas.crawl_job import (
    CrawlEnvironmentResponse,
    CrawlJobCreate,
    CrawlJobListData,
    CrawlJobResponse,
    CrawlLogData,
    CrawlNoteResponse,
    CrawlResultsData,
)
from app.services.crawler_env import probe_environment

router = APIRouter(prefix="/crawl", tags=["素材抓取"])
logger = get_logger(__name__)


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
    return ApiResponse(data=CrawlJobResponse.from_model(job))


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


@router.delete(
    "/jobs/{job_id}",
    response_model=ApiResponse[dict],
    summary="删除素材抓取任务记录",
)
def delete_job(
    service: CrawlJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[dict]:
    """删除任务记录；输出目录里的 jsonl 与媒体文件保留不动。"""
    service.delete_job(job_id)
    return ApiResponse(data={"id": job_id})
