"""一键成品接口。

路由注册顺序：/environment、/settings、/sources、
/copy-jobs/batch-delete、/render-jobs/batch-delete 等静态路径
必须写在 /copy-jobs/{job_id} 与 /render-jobs/{job_id} 之前。

「任意本地视频的只读预览流」不在这儿 —— 它是跨功能的（一键成品的框选步骤、
镜头分割/字幕提取的素材列表都要播本地视频），统一放在 `GET /fs/preview?path=`，
见 api/v1/fs.py。

接口一览：
- GET  /finalcut/environment              环境自检（AI / ffmpeg-drawtext / 中文字体）
- GET  /finalcut/settings                 读 AI 配置（key 只给掩码）
- PUT  /finalcut/settings                 写 AI 配置（写回 .env 并热同步；key 留空 = 不改）
- GET  /finalcut/sources                  历史产物来源（混剪成片 + 字幕 .srt）
- POST /finalcut/copy-jobs                创建文案生成任务
- GET  /finalcut/copy-jobs                历史任务分页列表
- GET  /finalcut/copy-jobs/{id}           任务详情（轮询进度也用它）
- POST /finalcut/copy-jobs/{id}/cancel    取消任务（结果不落库）
- PUT  /finalcut/copy-jobs/{id}/remark    更新任务备注（空串 = 清空）
- POST /finalcut/copy-jobs/batch-delete   批量删除任务记录（无磁盘产物，无 purge）
- DELETE /finalcut/copy-jobs/{id}         删除任务记录（只删记录）
- POST /finalcut/render-jobs              创建合成任务（勾选的文案 × 框选 × 样式）
- GET  /finalcut/render-jobs              合成任务分页列表
- GET  /finalcut/render-jobs/{id}         合成任务详情（含每条成片）
- GET  /finalcut/render-jobs/{id}/outputs/{n}/video  成片视频流（支持 Range）
- GET  /finalcut/render-jobs/{id}/outputs/{n}/thumb  成片封面
- POST /finalcut/render-jobs/{id}/cancel  取消合成任务
- PUT  /finalcut/render-jobs/{id}/remark  更新任务备注（空串 = 清空）
- POST /finalcut/render-jobs/batch-delete 批量删除（purge_files=true 连产物一起删）
- DELETE /finalcut/render-jobs/{id}       删除合成任务（?purge_files=true 一并清产物）
"""

from fastapi import APIRouter, Path as PathParam, Query, Request
from fastapi.responses import FileResponse, Response

from app.api.deps import FinalcutCopyJobServiceDep, FinalcutRenderJobServiceDep
from app.core.exceptions import BadRequestError, NotFoundError
from app.core.logging import get_logger
from app.schemas.common import ApiResponse, JobBatchDeleteRequest, JobRemarkUpdate
from app.schemas.finalcut_job import (
    AiSettingsResponse,
    AiSettingsUpdate,
    FinalcutCopyJobCreate,
    FinalcutCopyJobListData,
    FinalcutCopyJobResponse,
    FinalcutEnvironmentResponse,
    FinalcutRenderJobCreate,
    FinalcutRenderJobListData,
    FinalcutRenderJobResponse,
    FinalcutSourceItem,
    FinalcutSourcesResponse,
)
from app.services import ai_settings
from app.services.file_range import ranged_file_response
from app.services.finalcut_env import probe_environment
from app.services.media_tools import generate_thumbnail

router = APIRouter(prefix="/finalcut", tags=["一键成品"])
logger = get_logger(__name__)


# --------------------------------------------------------------------------
# 环境自检与 AI 配置
# --------------------------------------------------------------------------


@router.get(
    "/environment",
    response_model=ApiResponse[FinalcutEnvironmentResponse],
    summary="一键成品运行环境自检",
)
def get_environment(
    refresh: bool = Query(default=False, description="重新检测（与其它模块接口形状一致；本探测无缓存）"),
) -> ApiResponse[FinalcutEnvironmentResponse]:
    """探测 AI 配置、ffmpeg 的 drawtext 能力与中文字体，缺什么给什么修复指引。

    顺手做一次「从 .env 同步 AI 配置」：用户手改了 .env 没重启时，
    点「重新检测」就能捡起来（与字幕提取的重新检测同一语义）。
    """
    if refresh:
        ai_settings.sync_from_env_file()
    return ApiResponse(data=FinalcutEnvironmentResponse(**probe_environment(refresh=refresh)))


@router.get(
    "/settings",
    response_model=ApiResponse[AiSettingsResponse],
    summary="读取 AI 配置",
)
def get_ai_settings() -> ApiResponse[AiSettingsResponse]:
    """读当前生效的 AI 配置。API key 只给「有没有 + 掩码」，完整值不出后端。"""
    return ApiResponse(data=AiSettingsResponse(**ai_settings.read_ai_settings()))


@router.put(
    "/settings",
    response_model=ApiResponse[AiSettingsResponse],
    summary="保存 AI 配置",
)
def put_ai_settings(payload: AiSettingsUpdate) -> ApiResponse[AiSettingsResponse]:
    """把 AI 配置写回 backend/.env 并热同步进当前进程。

    api_key 留空表示保持原值（读接口只给掩码，页面回填不了原值，
    留空必须等于不动）。`.env` 被编辑器占用时抛 409，提示关闭后重试。
    """
    try:
        ai_settings.write_ai_settings(
            base_url=payload.base_url, model=payload.model, api_key=payload.api_key
        )
    except ValueError as exc:
        # 空值或值里带了换行/引号之类写不进 .env 的字符（照字幕设置的先例翻成 400）
        raise BadRequestError(str(exc)) from exc
    ai_settings.sync_from_env_file()
    return ApiResponse(data=AiSettingsResponse(**ai_settings.read_ai_settings()))


# --------------------------------------------------------------------------
# 文案生成任务
# --------------------------------------------------------------------------


@router.post(
    "/copy-jobs",
    response_model=ApiResponse[FinalcutCopyJobResponse],
    status_code=201,
    summary="创建文案生成任务",
)
def create_copy_job(
    payload: FinalcutCopyJobCreate,
    service: FinalcutCopyJobServiceDep,
) -> ApiResponse[FinalcutCopyJobResponse]:
    """创建任务并排入队列，AI 调用由后台工作线程执行。

    路径在创建时校验并固化进任务记录（文件存在、视频能探出时长、
    字幕有可用文本），之后只认记录里的路径。
    """
    job = service.create_job(payload)
    return ApiResponse(data=FinalcutCopyJobResponse.from_model(job))


@router.get(
    "/copy-jobs",
    response_model=ApiResponse[FinalcutCopyJobListData],
    summary="分页查询文案任务",
)
def list_copy_jobs(
    service: FinalcutCopyJobServiceDep,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=10, ge=1, le=100, description="每页条数，最大 100"),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="按状态过滤：pending/running/success/failed/cancelled",
    ),
) -> ApiResponse[FinalcutCopyJobListData]:
    """分页查询历史任务。"""
    items, total = service.list_jobs(page=page, page_size=page_size, status=status_filter)
    return ApiResponse(
        data=FinalcutCopyJobListData(
            total=total,
            page=page,
            page_size=page_size,
            items=[FinalcutCopyJobResponse.from_model(item) for item in items],
        )
    )


# 静态路径 /copy-jobs/batch-delete 必须写在 /copy-jobs/{job_id} 之前
@router.post(
    "/copy-jobs/batch-delete",
    response_model=ApiResponse[dict],
    summary="批量删除文案任务记录",
)
def batch_delete_copy_jobs(
    payload: JobBatchDeleteRequest,
    service: FinalcutCopyJobServiceDep,
) -> ApiResponse[dict]:
    """整批删除（全成功或全失败）。文案任务磁盘上无产物，purge_files 忽略。"""
    ids = service.delete_jobs(payload.ids)
    return ApiResponse(data={"ids": ids, "count": len(ids)})


@router.get(
    "/copy-jobs/{job_id}",
    response_model=ApiResponse[FinalcutCopyJobResponse],
    summary="获取文案任务详情",
)
def get_copy_job(
    service: FinalcutCopyJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[FinalcutCopyJobResponse]:
    """按 ID 获取任务详情（含生成结果），前端轮询进度也调它。"""
    job = service.get_job(job_id)
    return ApiResponse(data=FinalcutCopyJobResponse.from_model(job))


@router.post(
    "/copy-jobs/{job_id}/cancel",
    response_model=ApiResponse[FinalcutCopyJobResponse],
    summary="取消文案任务",
)
def cancel_copy_job(
    service: FinalcutCopyJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[FinalcutCopyJobResponse]:
    """取消任务：结果不会落库。在途的 AI 请求会跑完再丢弃（上界 AI_TIMEOUT_SECONDS）。"""
    job = service.cancel_job(job_id)
    return ApiResponse(data=FinalcutCopyJobResponse.from_model(job))


@router.put(
    "/copy-jobs/{job_id}/remark",
    response_model=ApiResponse[FinalcutCopyJobResponse],
    summary="更新文案任务备注",
)
def update_copy_job_remark(
    payload: JobRemarkUpdate,
    service: FinalcutCopyJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[FinalcutCopyJobResponse]:
    """更新任务备注，空串表示清空。"""
    job = service.update_remark(job_id, payload)
    return ApiResponse(data=FinalcutCopyJobResponse.from_model(job))


@router.delete(
    "/copy-jobs/{job_id}",
    response_model=ApiResponse[dict],
    summary="删除文案任务记录",
)
def delete_copy_job(
    service: FinalcutCopyJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[dict]:
    """删除任务记录。文案任务磁盘上无产物，删的就是记录本身（无 purge 参数）。"""
    service.delete_job(job_id)
    return ApiResponse(data={"id": job_id})


# --------------------------------------------------------------------------
# 素材来源与本地视频预览
# --------------------------------------------------------------------------


@router.get(
    "/sources",
    response_model=ApiResponse[FinalcutSourcesResponse],
    summary="历史产物来源清单",
)
def get_sources(
    service: FinalcutRenderJobServiceDep,
) -> ApiResponse[FinalcutSourcesResponse]:
    """混剪的成片与字幕任务的 .srt，一键成品的「选素材」步骤从这里挑。"""
    data = service.list_sources()
    return ApiResponse(
        data=FinalcutSourcesResponse(
            subtitles=[FinalcutSourceItem(**item) for item in data["subtitles"]],
            videos=[FinalcutSourceItem(**item) for item in data["videos"]],
        )
    )


# --------------------------------------------------------------------------
# 合成任务
# --------------------------------------------------------------------------


@router.post(
    "/render-jobs",
    response_model=ApiResponse[FinalcutRenderJobResponse],
    status_code=201,
    summary="创建合成任务",
)
def create_render_job(
    payload: FinalcutRenderJobCreate,
    service: FinalcutRenderJobServiceDep,
) -> ApiResponse[FinalcutRenderJobResponse]:
    """创建合成任务并排入队列：勾选的每条文案各产一个成片。

    文案、框选、样式在创建时固化成 item 快照 —— 之后删掉来源文案任务
    也不影响这个任务（copy_job_id 仅溯源）。
    """
    job = service.create_job(payload)
    return ApiResponse(data=FinalcutRenderJobResponse.from_model(job))


@router.get(
    "/render-jobs",
    response_model=ApiResponse[FinalcutRenderJobListData],
    summary="分页查询合成任务",
)
def list_render_jobs(
    service: FinalcutRenderJobServiceDep,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=10, ge=1, le=100, description="每页条数，最大 100"),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="按状态过滤：pending/running/success/partial/failed/cancelled",
    ),
) -> ApiResponse[FinalcutRenderJobListData]:
    """分页查询历史任务，列表不携带每条成片的明细。"""
    items, total = service.list_jobs(page=page, page_size=page_size, status=status_filter)
    return ApiResponse(
        data=FinalcutRenderJobListData(
            total=total,
            page=page,
            page_size=page_size,
            items=[
                FinalcutRenderJobResponse.from_model(item, include_items=False)
                for item in items
            ],
        )
    )


# 静态路径 /render-jobs/batch-delete 必须写在 /render-jobs/{job_id} 之前
@router.post(
    "/render-jobs/batch-delete",
    response_model=ApiResponse[dict],
    summary="批量删除合成任务记录",
)
def batch_delete_render_jobs(
    payload: JobBatchDeleteRequest,
    service: FinalcutRenderJobServiceDep,
) -> ApiResponse[dict]:
    """整批删除（全成功或全失败）；purge_files=true 时输出目录一并清掉。"""
    ids = service.delete_jobs(payload.ids, purge_files=payload.purge_files)
    return ApiResponse(data={"ids": ids, "count": len(ids)})


@router.get(
    "/render-jobs/{job_id}",
    response_model=ApiResponse[FinalcutRenderJobResponse],
    summary="获取合成任务详情",
)
def get_render_job(
    service: FinalcutRenderJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[FinalcutRenderJobResponse]:
    """按 ID 获取任务详情（含每条成片的结果），前端轮询进度也调它。"""
    job = service.get_job(job_id)
    return ApiResponse(data=FinalcutRenderJobResponse.from_model(job))


@router.get(
    "/render-jobs/{job_id}/outputs/{output_index}/video",
    summary="成片视频流（支持 Range）",
)
def get_render_output_video(
    request: Request,
    service: FinalcutRenderJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    output_index: int = PathParam(..., ge=1, description="成片序号（从 1 开始）"),
) -> Response:
    """在线播放成片：整文件（200）或字节段（206 Partial Content）。

    安全模型与混剪一致：只收「任务 ID + 成片序号」，路径完全由任务记录
    推导，不接受任何外部传入的路径。
    """
    output_path, _name = service.get_output_path(job_id, output_index)
    return ranged_file_response(
        output_path,
        request.headers.get("range"),
        media_type="video/mp4",
    )


@router.get(
    "/render-jobs/{job_id}/outputs/{output_index}/thumb",
    summary="成片封面",
    response_class=FileResponse,
)
def get_render_output_thumb(
    service: FinalcutRenderJobServiceDep,
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
    "/render-jobs/{job_id}/cancel",
    response_model=ApiResponse[FinalcutRenderJobResponse],
    summary="取消合成任务",
)
def cancel_render_job(
    service: FinalcutRenderJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[FinalcutRenderJobResponse]:
    """取消排队中或执行中的任务，执行中的会整组杀掉当前 ffmpeg 子进程。"""
    job = service.cancel_job(job_id)
    return ApiResponse(data=FinalcutRenderJobResponse.from_model(job))


@router.put(
    "/render-jobs/{job_id}/remark",
    response_model=ApiResponse[FinalcutRenderJobResponse],
    summary="更新合成任务备注",
)
def update_render_job_remark(
    payload: JobRemarkUpdate,
    service: FinalcutRenderJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[FinalcutRenderJobResponse]:
    """更新任务备注，空串表示清空。"""
    job = service.update_remark(job_id, payload)
    return ApiResponse(data=FinalcutRenderJobResponse.from_model(job))


@router.delete(
    "/render-jobs/{job_id}",
    response_model=ApiResponse[dict],
    summary="删除合成任务记录",
)
def delete_render_job(
    service: FinalcutRenderJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    purge_files: bool = Query(default=False, description="是否连同磁盘上的任务产物一起删除"),
) -> ApiResponse[dict]:
    """删除任务记录（级联删成片条目）；默认保留磁盘产物，purge_files=true 时一并清掉。"""
    service.delete_job(job_id, purge_files=purge_files)
    return ApiResponse(data={"id": job_id})
