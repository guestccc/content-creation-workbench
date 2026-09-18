"""智能混剪接口。

路由注册顺序：/environment、/sources、/library 等静态路径必须写在 /{job_id} 之前。

接口一览：
- GET  /environment              运行环境自检（ffmpeg / ffprobe + 素材目录）
- GET  /sources                  已添加的素材目录清单
- POST /sources                  添加素材目录（任意本地目录）
- DELETE /sources/{id}           移除素材目录（磁盘文件不动）
- GET  /library                  素材扫描结果（素材目录 + 素材 + 时长）
- GET  /library/clips/{id}/thumb 素材缩略图（首次访问时抽帧生成）
- GET  /library/clips/{id}/video 素材视频流（支持 Range，可拖动进度条）
- POST /jobs                     创建任务
- GET  /jobs                     历史任务分页列表
- GET  /jobs/{id}                任务详情（轮询进度也用它）
- GET  /jobs/{id}/outputs/{n}/video  成片视频流（支持 Range）
- GET  /jobs/{id}/outputs/{n}/thumb  成片封面
- POST /jobs/{id}/cancel         取消任务
- DELETE /jobs/{id}              删除任务记录（成片文件保留）
"""

from fastapi import APIRouter, Path as PathParam, Query, Request
from fastapi.responses import FileResponse, Response

from app.api.deps import MixJobServiceDep
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.services.file_range import ranged_file_response
from app.services.media_tools import generate_thumbnail
from app.services.mix_library import list_sources, resolve_clip, thumb_path_for
from app.services.mix_runner import probe_environment
from app.schemas.common import ApiResponse
from app.schemas.mix_job import (
    MixEnvironmentResponse,
    MixJobCreate,
    MixJobListData,
    MixJobResponse,
    MixLibraryData,
    MixSourceCreate,
    MixSourceItem,
)

router = APIRouter(prefix="/mix", tags=["智能混剪"])
logger = get_logger(__name__)


@router.get(
    "/environment",
    response_model=ApiResponse[MixEnvironmentResponse],
    summary="混剪运行环境自检",
)
def get_environment() -> ApiResponse[MixEnvironmentResponse]:
    """探测 ffmpeg / ffprobe 是否可用，缺什么给什么修复命令。"""
    return ApiResponse(data=MixEnvironmentResponse(**probe_environment()))


@router.get(
    "/sources",
    response_model=ApiResponse[list[MixSourceItem]],
    summary="已添加的素材目录",
)
def get_sources() -> ApiResponse[list[MixSourceItem]]:
    """返回用户添加过的素材目录清单（不带素材明细）。"""
    return ApiResponse(data=[MixSourceItem(**item) for item in list_sources()])


@router.post(
    "/sources",
    response_model=ApiResponse[MixSourceItem],
    status_code=201,
    summary="添加素材目录",
)
def create_source(
    payload: MixSourceCreate,
    service: MixJobServiceDep,
) -> ApiResponse[MixSourceItem]:
    """把任意本地目录加进素材列表。

    只登记目录，不复制也不移动任何文件；重复添加同一个目录是幂等的
    （此时返回 201 但 created 语义上等同已有条目，前端拿到的是同一份信息）。

    Raises:
        BadRequestError(400): 路径为空/非绝对/不存在/不是目录/数量超限。
    """
    source, _created = service.add_source(payload.path)
    return ApiResponse(data=MixSourceItem(**source))


@router.delete(
    "/sources/{source_id}",
    response_model=ApiResponse[dict],
    summary="移除素材目录",
)
def delete_source(
    service: MixJobServiceDep,
    source_id: str = PathParam(..., description="素材目录 id"),
) -> ApiResponse[dict]:
    """把目录从素材列表里移除。

    **磁盘上的文件一个都不动** —— 这只是「不再从这个目录取素材」，
    不是删除素材。已经创建的任务也不受影响（任务里存的是绝对路径）。
    """
    service.remove_source(source_id)
    return ApiResponse(data={"id": source_id})


@router.get(
    "/library",
    response_model=ApiResponse[MixLibraryData],
    summary="素材扫描",
)
def get_library(
    service: MixJobServiceDep,
) -> ApiResponse[MixLibraryData]:
    """扫描所有已添加的素材目录，返回目录与素材清单（时长来自缓存，秒开）。"""
    return ApiResponse(data=MixLibraryData(**service.get_library()))


@router.get(
    "/library/clips/{clip_id}/thumb",
    summary="素材缩略图",
    response_class=FileResponse,
)
def get_clip_thumb(
    clip_id: str = PathParam(..., description="片段 id"),
) -> FileResponse:
    """返回素材首帧缩略图。

    安全说明：接口只接受片段 id（sha1），服务端重新扫描比对后才返回文件 ——
    如果放开让前端传路径，这就是一个任意文件读取漏洞。
    首次访问时用 ffmpeg 抽帧，缓存统一落在素材根的 .mix-thumbs/ 下 ——
    素材目录可能是用户的任意目录，不往人家自己的目录里写东西。
    """
    clip_path = resolve_clip(clip_id)
    if clip_path is None:
        raise NotFoundError(f"素材不存在、已被移动或已移出素材目录：{clip_id}")
    thumb_path = thumb_path_for(clip_id)
    if not thumb_path.is_file():
        if not generate_thumbnail(clip_path, thumb_path):
            raise NotFoundError(f"缩略图生成失败（ffmpeg 不可用或素材损坏）：{clip_path.name}")
    return FileResponse(thumb_path, media_type="image/jpeg")


@router.get(
    "/library/clips/{clip_id}/video",
    summary="素材视频流（支持 Range）",
)
def get_clip_video(
    request: Request,
    clip_id: str = PathParam(..., description="片段 id"),
) -> Response:
    """在线播放素材：整文件（200）或字节段（206 Partial Content）。

    安全模型与缩略图一致：只收片段 id，路径完全由服务端扫描推导。
    """
    clip_path = resolve_clip(clip_id)
    if clip_path is None:
        raise NotFoundError(f"素材不存在、已被移动或已移出素材目录：{clip_id}")
    return ranged_file_response(
        clip_path,
        request.headers.get("range"),
        media_type="video/mp4",
    )


@router.post(
    "/jobs",
    response_model=ApiResponse[MixJobResponse],
    status_code=201,
    summary="创建混剪任务",
)
def create_job(
    payload: MixJobCreate,
    service: MixJobServiceDep,
) -> ApiResponse[MixJobResponse]:
    """创建任务并排入队列，真正的合成由后台工作线程执行。"""
    job = service.create_job(payload)
    return ApiResponse(data=MixJobResponse.from_model(job))


@router.get(
    "/jobs",
    response_model=ApiResponse[MixJobListData],
    summary="分页查询混剪任务",
)
def list_jobs(
    service: MixJobServiceDep,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=10, ge=1, le=100, description="每页条数，最大 100"),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="按状态过滤：pending/running/success/partial/failed/cancelled",
    ),
) -> ApiResponse[MixJobListData]:
    """分页查询历史任务，列表不携带每条成片的明细。"""
    items, total = service.list_jobs(page=page, page_size=page_size, status=status_filter)
    return ApiResponse(
        data=MixJobListData(
            total=total,
            page=page,
            page_size=page_size,
            items=[MixJobResponse.from_model(item, include_outputs=False) for item in items],
        )
    )


@router.get(
    "/jobs/{job_id}",
    response_model=ApiResponse[MixJobResponse],
    summary="获取任务详情",
)
def get_job(
    service: MixJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[MixJobResponse]:
    """按 ID 获取任务详情（含每条成片的结果），前端轮询进度也调它。"""
    job = service.get_job(job_id)
    return ApiResponse(data=MixJobResponse.from_model(job))


@router.get(
    "/jobs/{job_id}/outputs/{output_index}/video",
    summary="成片视频流（支持 Range）",
)
def get_output_video(
    request: Request,
    service: MixJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    output_index: int = PathParam(..., ge=1, description="成片序号（从 1 开始）"),
) -> Response:
    """在线播放成片：整文件（200）或字节段（206 Partial Content）。

    安全模型与镜头分割一致：只收「任务 ID + 成片序号」，路径完全由
    任务记录推导，不接受任何外部传入的路径。
    """
    output_path, _name = service.get_output_path(job_id, output_index)
    return ranged_file_response(
        output_path,
        request.headers.get("range"),
        media_type="video/mp4",
    )


@router.get(
    "/jobs/{job_id}/outputs/{output_index}/thumb",
    summary="成片封面",
    response_class=FileResponse,
)
def get_output_thumb(
    service: MixJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    output_index: int = PathParam(..., ge=1, description="成片序号（从 1 开始）"),
) -> FileResponse:
    """返回成片首帧封面（首次访问时抽帧生成，之后读缓存）。"""
    output_path, output_name = service.get_output_path(job_id, output_index)
    thumb_path = output_path.parent / ".thumbs" / f"{output_path.stem}.jpg"
    if not thumb_path.is_file():
        if not generate_thumbnail(output_path, thumb_path):
            raise NotFoundError(f"封面生成失败（ffmpeg 不可用或成片损坏）：{output_name}")
    return FileResponse(thumb_path, media_type="image/jpeg")


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=ApiResponse[MixJobResponse],
    summary="取消混剪任务",
)
def cancel_job(
    service: MixJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[MixJobResponse]:
    """取消排队中或执行中的任务，执行中的会整组杀掉当前 ffmpeg 子进程。"""
    job = service.cancel_job(job_id)
    return ApiResponse(data=MixJobResponse.from_model(job))


@router.delete(
    "/jobs/{job_id}",
    response_model=ApiResponse[dict],
    summary="删除混剪任务记录",
)
def delete_job(
    service: MixJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[dict]:
    """删除任务记录（级联删成片条目）；磁盘上已拼出的成片文件保留不动。"""
    service.delete_job(job_id)
    return ApiResponse(data={"id": job_id})
