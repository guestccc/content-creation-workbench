"""视频字幕提取接口。

路由注册顺序说明：/environment 等静态路径必须写在 /{job_id} 之前，
否则会被路径参数吞掉返回 422。

接口一览：
- GET  /environment              运行环境自检（VideoCaptioner / ffmpeg / 安装指引）
- POST /jobs                     创建字幕提取任务
- GET  /jobs                     历史任务分页列表
- GET  /jobs/{id}                任务详情（轮询进度也用它）
- GET  /jobs/{id}/subtitles      任务产出的字幕文件列表
- GET  /jobs/{id}/subtitles/{n}  单份字幕的文本内容（预览用，超长截断）
- POST /jobs/{id}/cancel         取消任务
- DELETE /jobs/{id}              删除任务记录
"""

from fastapi import APIRouter, Path as PathParam, Query

from app.api.deps import SubtitleJobServiceDep
from app.core.config import settings
from app.core.logging import get_logger
from app.models.subtitle_job import SubtitleJobStatus
from app.schemas.common import ApiResponse
from app.schemas.subtitle_job import (
    SubtitleEnvironmentResponse,
    SubtitleFileResponse,
    SubtitleJobCreate,
    SubtitleJobListData,
    SubtitleJobResponse,
    SubtitleTextData,
    engines_payload_response,
)
from app.services.subtitle_env import probe_environment

router = APIRouter(prefix="/subtitle", tags=["视频字幕提取"])
logger = get_logger(__name__)


@router.get(
    "/environment",
    response_model=ApiResponse[SubtitleEnvironmentResponse],
    summary="字幕提取运行环境自检",
)
def get_environment(
    refresh: bool = Query(default=False, description="绕过探测缓存重新检测"),
) -> ApiResponse[SubtitleEnvironmentResponse]:
    """探测 VideoCaptioner 与 ffmpeg 是否可用，未安装时给出按平台的安装指引。

    指引只是文本，后端绝不替用户执行安装 —— 网页触发的安装命令既不可靠
    （权限、网络、杀毒软件），也不该有（网页进程不该有装软件的权力）。
    """
    env = probe_environment(refresh=refresh)
    return ApiResponse(
        data=SubtitleEnvironmentResponse(**env, asr_engines=engines_payload_response())
    )


@router.post(
    "/jobs",
    response_model=ApiResponse[SubtitleJobResponse],
    status_code=201,
    summary="创建字幕提取任务",
)
def create_job(
    payload: SubtitleJobCreate,
    service: SubtitleJobServiceDep,
) -> ApiResponse[SubtitleJobResponse]:
    """创建任务并排入队列，真正的转写由后台工作线程执行。"""
    job = service.create_job(payload)
    return ApiResponse(data=SubtitleJobResponse.from_model(job))


@router.get(
    "/jobs",
    response_model=ApiResponse[SubtitleJobListData],
    summary="分页查询字幕提取任务",
)
def list_jobs(
    service: SubtitleJobServiceDep,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=10, ge=1, le=100, description="每页条数，最大 100"),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description=f"按状态过滤：{'/'.join(SubtitleJobStatus.ALL)}",
    ),
) -> ApiResponse[SubtitleJobListData]:
    """分页查询历史任务，列表不携带每条视频的明细。"""
    items, total = service.list_jobs(page=page, page_size=page_size, status=status_filter)
    return ApiResponse(
        data=SubtitleJobListData(
            total=total,
            page=page,
            page_size=page_size,
            items=[SubtitleJobResponse.from_model(item, include_items=False) for item in items],
        )
    )


@router.get(
    "/jobs/{job_id}",
    response_model=ApiResponse[SubtitleJobResponse],
    summary="获取字幕提取任务详情",
)
def get_job(
    service: SubtitleJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[SubtitleJobResponse]:
    """按 ID 获取任务详情（含每条视频的结果），前端轮询进度也调它。"""
    job = service.get_job(job_id)
    return ApiResponse(data=SubtitleJobResponse.from_model(job))


@router.get(
    "/jobs/{job_id}/subtitles",
    response_model=ApiResponse[list],
    summary="任务产出的字幕文件列表",
)
def list_subtitles(
    service: SubtitleJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[list]:
    """列出任务已产出的字幕文件（尚未生成的不返回，前端从任务详情的条目状态看进度）。"""
    subtitles = service.list_subtitles(job_id)
    return ApiResponse(
        data=[
            SubtitleFileResponse(
                index=item["index"],
                item_index=item["item_index"],
                name=item["name"],
                source_name=item["source_name"],
                size_bytes=item["size_bytes"],
                segment_count=item["segment_count"],
            ).model_dump()
            for item in subtitles
            if item["exists"]
        ]
    )


@router.get(
    "/jobs/{job_id}/subtitles/{index}",
    response_model=ApiResponse[SubtitleTextData],
    summary="单份字幕的文本内容（预览）",
)
def get_subtitle_text(
    service: SubtitleJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    index: int = PathParam(..., ge=1, description="字幕序号（全局，从 1 开始）"),
) -> ApiResponse[SubtitleTextData]:
    """返回一份 .srt 的文本内容，超过大小上限时只返回开头一段。

    安全说明：接口只接受「任务 ID + 字幕序号」，文件路径完全由任务记录
    推导 —— 如果放开让前端传路径，这就是一个任意文件读取漏洞。
    长视频的字幕可能有几百 KB，一次性返回会把响应撑爆，所以按
    SUBTITLE_PREVIEW_MAX_BYTES 截断并打上 truncated 标记，前端据此提示
    「只显示了开头」并引导用户去目录里看完整文件。
    """
    path, name, source_name = service.get_subtitle_path(job_id, index)
    size_bytes = path.stat().st_size

    max_bytes = settings.SUBTITLE_PREVIEW_MAX_BYTES
    truncated = size_bytes > max_bytes
    # 按字节截断后再解码，避免一次读入超大文件；srt 是 utf-8（可能带 BOM）
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1 if truncated else max_bytes)
    content = raw[:max_bytes].decode("utf-8-sig", errors="replace")
    if truncated:
        # 砍掉最后一个可能截断到一半的字幕块，让预览结尾是完整的
        last_boundary = content.rfind("\n\n")
        if last_boundary > 0:
            content = content[:last_boundary]

    return ApiResponse(
        data=SubtitleTextData(
            index=index,
            name=name,
            source_name=source_name,
            content=content,
            size_bytes=size_bytes,
            truncated=truncated,
        )
    )


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=ApiResponse[SubtitleJobResponse],
    summary="取消字幕提取任务",
)
def cancel_job(
    service: SubtitleJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[SubtitleJobResponse]:
    """取消排队中或执行中的任务，执行中的会整组杀掉当前转写子进程。"""
    job = service.cancel_job(job_id)
    return ApiResponse(data=SubtitleJobResponse.from_model(job))


@router.delete(
    "/jobs/{job_id}",
    response_model=ApiResponse[dict],
    summary="删除字幕提取任务记录",
)
def delete_job(
    service: SubtitleJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[dict]:
    """删除任务记录（级联删条目）；磁盘上已生成的字幕文件保留不动。"""
    service.delete_job(job_id)
    return ApiResponse(data={"id": job_id})
