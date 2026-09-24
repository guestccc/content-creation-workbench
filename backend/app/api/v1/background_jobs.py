"""一键换背景接口。

路由注册顺序说明：静态路径（/jobs/batch-delete）必须写在 /jobs/{job_id} 之前，
否则会被路径参数吞掉返回 422。

接口一览：
- POST   /jobs                        创建换背景任务
- GET    /jobs                        历史任务分页列表（可按 status / source_crawl_job_id 过滤）
- POST   /jobs/batch-delete           批量删除任务记录
- GET    /jobs/{id}                   任务详情（轮询进度也用它）
- GET    /jobs/{id}/items/{n}/output  单张产物 PNG（页面上直接当缩略图/大图显示）
- POST   /jobs/{id}/cancel            取消任务
- PUT    /jobs/{id}/remark            更新任务备注（空串 = 清空）
- DELETE /jobs/{id}                   删除任务记录

**产物为什么走任务级端点而不是 /fs/preview**：产物路径只在任务记录里，前端传不了
也猜不出；把产物交给 /fs/preview 等于给「按路径取任意文件」再加一个入口。这里的
序号由任务记录做边界检查，越界与未产出都回 404。
"""

from fastapi import APIRouter, Path as PathParam, Query
from fastapi.responses import FileResponse

from app.api.deps import BackgroundJobServiceDep
from app.core.logging import get_logger
from app.models.background_job import BackgroundJobStatus
from app.schemas.background_job import (
    BackgroundJobCreate,
    BackgroundJobListData,
    BackgroundJobResponse,
)
from app.schemas.common import ApiResponse, JobBatchDeleteRequest, JobRemarkUpdate

router = APIRouter(prefix="/background", tags=["一键换背景"])
logger = get_logger(__name__)


@router.post(
    "/jobs",
    response_model=ApiResponse[BackgroundJobResponse],
    status_code=201,
    summary="创建一键换背景任务",
)
def create_job(
    payload: BackgroundJobCreate,
    service: BackgroundJobServiceDep,
) -> ApiResponse[BackgroundJobResponse]:
    """创建任务并排入队列，真正的抠图 / 合成由后台工作线程执行。

    创建时就把能查的错一次性查完（背景图在不在、原图清单是否为空、输出目录能不能
    建），用户点击的那一刻就知道问题，而不是排队几分钟后整条失败。
    """
    job = service.create_job(payload)
    return ApiResponse(data=BackgroundJobResponse.from_model(job))


@router.get(
    "/jobs",
    response_model=ApiResponse[BackgroundJobListData],
    summary="分页查询一键换背景任务",
)
def list_jobs(
    service: BackgroundJobServiceDep,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=10, ge=1, le=100, description="每页条数，最大 100"),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description=f"按状态过滤：{'/'.join(BackgroundJobStatus.ALL)}",
    ),
    source_crawl_job_id: int | None = Query(
        default=None,
        ge=1,
        description="只列出来自该素材抓取任务的任务（素材抓取页把派生任务挂回笔记行用）",
    ),
) -> ApiResponse[BackgroundJobListData]:
    """分页查询历史任务，列表不携带每张图的明细。"""
    items, total = service.list_jobs(
        page=page,
        page_size=page_size,
        status=status_filter,
        source_crawl_job_id=source_crawl_job_id,
    )
    return ApiResponse(
        data=BackgroundJobListData(
            total=total,
            page=page,
            page_size=page_size,
            items=[
                BackgroundJobResponse.from_model(item, include_items=False) for item in items
            ],
        )
    )


# 静态路径 /jobs/batch-delete 必须写在 /jobs/{job_id} 之前（见文件头说明）
@router.post(
    "/jobs/batch-delete",
    response_model=ApiResponse[dict],
    summary="批量删除一键换背景任务记录",
)
def batch_delete_jobs(
    payload: JobBatchDeleteRequest,
    service: BackgroundJobServiceDep,
) -> ApiResponse[dict]:
    """整批删除（全成功或全失败）；purge_files=true 时产物目录一并清掉。"""
    ids = service.delete_jobs(payload.ids, purge_files=payload.purge_files)
    return ApiResponse(data={"ids": ids, "count": len(ids)})


@router.get(
    "/jobs/{job_id}",
    response_model=ApiResponse[BackgroundJobResponse],
    summary="获取一键换背景任务详情",
)
def get_job(
    service: BackgroundJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[BackgroundJobResponse]:
    """按 ID 获取任务详情（含每张图的结果与诊断统计），前端轮询进度也调它。"""
    job = service.get_job(job_id)
    return ApiResponse(data=BackgroundJobResponse.from_model(job))


@router.get(
    "/jobs/{job_id}/items/{index}/output",
    response_class=FileResponse,
    summary="单张产物图（PNG）",
)
def get_item_output(
    service: BackgroundJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    index: int = PathParam(..., ge=1, description="原图在任务内的序号，从 1 开始"),
) -> FileResponse:
    """返回某一张原图换完背景的 PNG。

    安全说明：只接受「任务 ID + 序号」，文件路径完全由任务记录推导 ——
    如果放开让前端传路径，这就是一个任意文件读取漏洞。序号越界、该张失败、
    文件被移走，都统一回 404（前端只关心「这张能不能显示」）。

    媒体类型写死 image/png 而不是按扩展名猜：产物一定是 PNG（算法输出带 alpha，
    jpg 存不了透明通道），写死省掉一次不可靠的推断。
    """
    path, _name, _source_name = service.get_output_path(job_id, index)
    return FileResponse(path, media_type="image/png")


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=ApiResponse[BackgroundJobResponse],
    summary="取消一键换背景任务",
)
def cancel_job(
    service: BackgroundJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[BackgroundJobResponse]:
    """取消排队中或执行中的任务。

    ⚠️ 执行中的任务**不是立刻停**：抠图是纯运算，没有子进程可杀，当前这张会
    算完才收手（延迟上界 = 单张图处理时间），剩余张标记为 skipped。
    """
    job = service.cancel_job(job_id)
    return ApiResponse(data=BackgroundJobResponse.from_model(job))


@router.put(
    "/jobs/{job_id}/remark",
    response_model=ApiResponse[BackgroundJobResponse],
    summary="更新一键换背景任务备注",
)
def update_job_remark(
    payload: JobRemarkUpdate,
    service: BackgroundJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[BackgroundJobResponse]:
    """更新任务备注，空串表示清空。"""
    job = service.update_remark(job_id, payload)
    return ApiResponse(data=BackgroundJobResponse.from_model(job, include_items=False))


@router.post(
    "/jobs/{job_id}/items/{index}/retry",
    response_model=ApiResponse[BackgroundJobResponse],
    summary="重试单张失败 / 跳过的原图",
)
def retry_job_item(
    service: BackgroundJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    index: int = PathParam(..., ge=1, description="原图在任务内的序号，从 1 开始"),
) -> ApiResponse[BackgroundJobResponse]:
    """把该张重置回 pending 并让任务重新入队，其余张的结果保持不动。

    任务还在排队 / 执行中时拒绝（409）—— 正在跑的任务重跑没有意义；产物不必
    手动清理，重试前会先删掉这一张的旧产物。
    """
    job = service.retry_item(job_id, index)
    return ApiResponse(data=BackgroundJobResponse.from_model(job))


@router.post(
    "/jobs/{job_id}/retry",
    response_model=ApiResponse[BackgroundJobResponse],
    summary="重试全部未完成的原图",
)
def retry_job_items(
    service: BackgroundJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
) -> ApiResponse[BackgroundJobResponse]:
    """把该任务所有「失败」「跳过」的原图一起重新入队，已成功的保持原样。

    走这一个接口而不是让前端循环调单张重试：第一次调用就会把任务置回 pending，
    第二次会撞上「任务尚未结束」的校验。
    """
    job = service.retry_items(job_id)
    return ApiResponse(data=BackgroundJobResponse.from_model(job))


@router.delete(
    "/jobs/{job_id}",
    response_model=ApiResponse[dict],
    summary="删除一键换背景任务记录",
)
def delete_job(
    service: BackgroundJobServiceDep,
    job_id: int = PathParam(..., ge=1, description="任务 ID"),
    purge_files: bool = Query(default=False, description="是否连同磁盘上的任务产物一起删除"),
) -> ApiResponse[dict]:
    """删除任务记录（级联删条目）；默认保留磁盘产物，purge_files=true 时一并清掉。"""
    service.delete_job(job_id, purge_files=purge_files)
    return ApiResponse(data={"id": job_id})
