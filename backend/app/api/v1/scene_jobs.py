"""智能镜头分割接口。

路由注册顺序说明：/templates、/environment 等静态路径必须写在 /{job_id}
之前，否则会被路径参数吞掉返回 422。

接口一览：
- GET  /templates           模板列表（前端只展示，参数真源在后端）
- GET  /environment         运行环境自检（vct / scenedetect / ffmpeg）
- POST /jobs                创建任务（preview 只检测切点，split 真正切割）
- GET  /jobs                历史任务分页列表
- GET  /jobs/{id}           任务详情（轮询进度也用它）
- GET  /jobs/{id}/scenes    预览切点汇总
- GET  /jobs/{id}/clips     切分产出的片段列表（含 item_index：归哪条视频，供前端分组）
- GET  /jobs/{id}/clips/{n}/thumb  片段缩略图（首次访问时抽帧生成）
- GET  /jobs/{id}/clips/{n}/video  片段视频流（支持 Range，可拖动进度条）
- POST /jobs/{id}/cancel    取消任务
- PUT  /jobs/{id}/remark    更新任务备注（空串 = 清空）
- POST /jobs/{id}/items/{n}/retry  重试单条失败 / 跳过的视频（条目重置回 pending，任务重新入队）
- POST /jobs/{id}/retry     重试全部失败 / 跳过的视频
- POST /jobs/batch-delete   批量删除任务记录（payload 带 purge_files 时产物一并清）
- DELETE /jobs/{id}         删除任务记录（?purge_files=true 时产物一并清）
"""

from pathlib import Path

from fastapi import APIRouter, Path as PathParam, Query, Request
from fastapi.responses import FileResponse, Response

from app.api.deps import SceneJobServiceDep
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.scene_templates import (
    CUSTOM_TEMPLATE_KEY,
    DETECTOR_DEFAULT_THRESHOLDS,
    DETECTORS,
    TEMPLATES,
)
from app.services.file_range import ranged_file_response
from app.services.scene_runner import generate_thumbnail, probe_environment
from app.models.scene_job import SceneJobMode, SceneJobStatus
from app.schemas.common import ApiResponse, JobBatchDeleteRequest, JobRemarkUpdate
from app.schemas.scene_job import (
    SceneClipResponse,
    SceneEnvironmentResponse,
    SceneJobCreate,
    SceneJobItemResponse,
    SceneJobListData,
    SceneJobResponse,
    SceneJobScenesData,
    SceneTemplateResponse,
)

router = APIRouter(prefix="/scene", tags=["智能镜头分割"])
logger = get_logger(__name__)


def _template_to_response(template) -> SceneTemplateResponse:
    """把模板定义转成接口响应（阈值文案在这里拼，前端不做逻辑）。"""
    if template.threshold is not None:
        threshold_label = str(template.threshold)
    else:
        threshold_label = f"默认({DETECTOR_DEFAULT_THRESHOLDS[template.detector]})"
    return SceneTemplateResponse(
        key=template.key,
        name=template.name,
        summary=template.summary,
        best_for=template.best_for,
        detector=template.detector,
        detector_label=DETECTORS[template.detector],
        threshold=template.threshold,
        threshold_label=threshold_label,
        min_len=template.min_len,
        copy_mode=template.copy_mode,
        recommended=template.recommended,
    )


@router.get(
    "/templates",
    response_model=ApiResponse[list],
    summary="镜头分割模板列表",
)
def list_templates() -> ApiResponse[list]:
    """获取全部预设模板，外加一个「自定义」占位项。"""
    data = [_template_to_response(item) for item in TEMPLATES]
    data.append(
        SceneTemplateResponse(
            key=CUSTOM_TEMPLATE_KEY,
            name="自定义",
            summary="检测器、阈值、最短镜头、切割方式全部自己定",
            best_for="熟悉参数含义后按需微调",
            detector="adaptive",
            detector_label=DETECTORS["adaptive"],
            threshold=None,
            threshold_label="自行指定",
            min_len=0.6,
            copy_mode=False,
            recommended=False,
        )
    )
    return ApiResponse(data=[item.model_dump() for item in data])


@router.get(
    "/environment",
    response_model=ApiResponse[SceneEnvironmentResponse],
    summary="镜头分割运行环境自检",
)
def get_environment() -> ApiResponse[SceneEnvironmentResponse]:
    """探测 vct / scenedetect / ffmpeg 是否可用，缺什么给什么修复命令。"""
    return ApiResponse(data=SceneEnvironmentResponse(**probe_environment()))


@router.post(
    "/jobs",
    response_model=ApiResponse[SceneJobResponse],
    status_code=201,
    summary="创建镜头分割任务",
)
def create_job(
    payload: SceneJobCreate,
    service: SceneJobServiceDep,
) -> ApiResponse[SceneJobResponse]:
    """创建任务并排入队列，真正的检测与切割由后台工作线程执行。"""
    job = service.create_job(payload)
    return ApiResponse(data=SceneJobResponse.from_model(job))


@router.get(
    "/jobs",
    response_model=ApiResponse[SceneJobListData],
    summary="分页查询镜头分割任务",
)
def list_jobs(
    service: SceneJobServiceDep,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=10, ge=1, le=100, description="每页条数，最大 100"),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description=f"按状态过滤：{'/'.join(SceneJobStatus.ALL)}",
    ),
    mode: str | None = Query(
        default=None,
        description=f"按模式过滤：{'/'.join(SceneJobMode.ALL)}",
    ),
) -> ApiResponse[SceneJobListData]:
    """分页查询历史任务，列表不携带每个视频的明细。"""
    items, total = service.list_jobs(
        page=page, page_size=page_size, status=status_filter, mode=mode
    )
    return ApiResponse(
        data=SceneJobListData(
            total=total,
            page=page,
            page_size=page_size,
            items=[SceneJobResponse.from_model(item, include_items=False) for item in items],
        )
    )


# 静态路径 /jobs/batch-delete 必须写在 /jobs/{job_id} 之前（见文件头说明）
@router.post(
    "/jobs/batch-delete",
    response_model=ApiResponse[dict],
    summary="批量删除镜头分割任务记录",
)
def batch_delete_jobs(
    payload: JobBatchDeleteRequest,
    service: SceneJobServiceDep,
) -> ApiResponse[dict]:
    """整批删除（全成功或全失败）；purge_files=true 时产物一并清掉。"""
    ids = service.delete_jobs(payload.ids, purge_files=payload.purge_files)
    return ApiResponse(data={"ids": ids, "count": len(ids)})


@router.get(
    "/jobs/{job_id}",
    response_model=ApiResponse[SceneJobResponse],
    summary="获取任务详情",
)
def get_job(
    service: SceneJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[SceneJobResponse]:
    """按 ID 获取任务详情（含每个视频的结果），前端轮询进度也调它。"""
    job = service.get_job(job_id)
    return ApiResponse(data=SceneJobResponse.from_model(job))


@router.get(
    "/jobs/{job_id}/scenes",
    response_model=ApiResponse[SceneJobScenesData],
    summary="预览切点汇总",
)
def get_scenes(
    service: SceneJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[SceneJobScenesData]:
    """汇总任务中所有视频的切点与镜头时长统计。"""
    summary = service.get_scenes_summary(job_id)
    return ApiResponse(
        data=SceneJobScenesData(
            job_id=summary["job_id"],
            status=summary["status"],
            total_scenes=summary["total_scenes"],
            total_duration=summary["total_duration"],
            shortest=summary["shortest"],
            longest=summary["longest"],
            average=summary["average"],
            items=[SceneJobItemResponse.from_model(item) for item in summary["items"]],
        )
    )


@router.get(
    "/jobs/{job_id}/clips",
    response_model=ApiResponse[list],
    summary="任务切分片段列表",
)
def list_clips(
    service: SceneJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[list]:
    """列出任务切出的全部片段，thumb_url 指向按需生成的缩略图接口。"""
    clips = service.list_clips(job_id)
    return ApiResponse(
        data=[
            SceneClipResponse(
                index=clip["index"],
                item_index=clip["item_index"],
                name=clip["name"],
                source_name=clip["source_name"],
                size_bytes=clip["size_bytes"],
                width=clip.get("width"),
                height=clip.get("height"),
                thumb_url=f"/api/v1/scene/jobs/{job_id}/clips/{clip['index']}/thumb",
                video_url=f"/api/v1/scene/jobs/{job_id}/clips/{clip['index']}/video",
            ).model_dump()
            for clip in clips
        ]
    )


@router.get(
    "/jobs/{job_id}/clips/{clip_index}/thumb",
    summary="片段缩略图",
    response_class=FileResponse,
)
def get_clip_thumb(
    service: SceneJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    clip_index: int = PathParam(..., ge=1, description="片段序号（全局，从 1 开始）"),
) -> FileResponse:
    """返回片段首帧缩略图。

    安全说明：接口只接受「任务 ID + 片段序号」，文件路径完全由任务记录
    推导 —— 如果放开让前端传路径，这就是一个任意文件读取漏洞。
    首次访问时用 ffmpeg 抽帧落盘缓存，之后直接读缓存。
    """
    clip_path, clip_name = service.get_clip_path(job_id, clip_index)
    thumb_path = clip_path.parent / ".thumbs" / f"{clip_path.stem}.jpg"

    if not thumb_path.is_file():
        if not generate_thumbnail(clip_path, thumb_path):
            raise NotFoundError(f"缩略图生成失败（ffmpeg 不可用或片段损坏）：{clip_name}")

    return FileResponse(thumb_path, media_type="image/jpeg")


@router.get(
    "/jobs/{job_id}/clips/{clip_index}/video",
    summary="片段视频流（支持 Range）",
)
def get_clip_video(
    request: Request,
    service: SceneJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    clip_index: int = PathParam(..., ge=1, description="片段序号（全局，从 1 开始）"),
) -> Response:
    """在线播放片段：整文件（200）或字节段（206 Partial Content）。

    浏览器拖进度条靠的就是 Range 请求 —— 没有这个接口的话 <video> 只能
    从头发。安全模型与缩略图一致：只收「任务 ID + 片段序号」，路径完全
    由任务记录推导，不接受任何外部传入的路径。
    """
    clip_path, _clip_name = service.get_clip_path(job_id, clip_index)
    return ranged_file_response(
        clip_path,
        request.headers.get("range"),
        media_type="video/mp4",
    )


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=ApiResponse[SceneJobResponse],
    summary="取消镜头分割任务",
)
def cancel_job(
    service: SceneJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[SceneJobResponse]:
    """取消排队中或执行中的任务，执行中的会整组杀掉当前 vct/ffmpeg 子进程。"""
    job = service.cancel_job(job_id)
    return ApiResponse(data=SceneJobResponse.from_model(job))


@router.put(
    "/jobs/{job_id}/remark",
    response_model=ApiResponse[SceneJobResponse],
    summary="更新镜头分割任务备注",
)
def update_job_remark(
    payload: JobRemarkUpdate,
    service: SceneJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[SceneJobResponse]:
    """更新任务备注，空串表示清空。"""
    job = service.update_remark(job_id, payload)
    return ApiResponse(data=SceneJobResponse.from_model(job, include_items=False))


@router.post(
    "/jobs/{job_id}/items/{item_index}/retry",
    response_model=ApiResponse[SceneJobResponse],
    summary="重试单条失败 / 跳过的视频",
)
def retry_job_item(
    service: SceneJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    item_index: int = PathParam(..., ge=1, description="条目序号（任务内，从 1 开始）"),
) -> ApiResponse[SceneJobResponse]:
    """把该条重置回 pending 并让任务重新入队，其余条目的结果保持不动。

    任务还在排队 / 执行中时拒绝（409）—— 正在跑的任务重跑没有意义；该条上次的
    片段产物会先清空，免得残留把重跑的进度与产物数一起污染。
    """
    job = service.retry_item(job_id, item_index)
    return ApiResponse(data=SceneJobResponse.from_model(job))


@router.post(
    "/jobs/{job_id}/retry",
    response_model=ApiResponse[SceneJobResponse],
    summary="重试全部未完成的视频",
)
def retry_job_items(
    service: SceneJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[SceneJobResponse]:
    """把该任务所有「失败」「跳过」的视频一起重新入队，已成功的保持原样。

    走这一个接口而不是让前端循环调单条重试：第一次调用就会把任务置回 pending，
    第二次会撞上「任务尚未结束」的校验。
    """
    job = service.retry_items(job_id)
    return ApiResponse(data=SceneJobResponse.from_model(job))


@router.delete(
    "/jobs/{job_id}",
    response_model=ApiResponse[dict],
    summary="删除镜头分割任务记录",
)
def delete_job(
    service: SceneJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    purge_files: bool = Query(default=False, description="是否连同磁盘上的任务产物一起删除"),
) -> ApiResponse[dict]:
    """删除任务记录（级联删条目）；默认保留磁盘产物，purge_files=true 时一并清掉。"""
    service.delete_job(job_id, purge_files=purge_files)
    return ApiResponse(data={"id": job_id})
