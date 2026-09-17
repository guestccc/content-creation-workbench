"""发布任务接口。

路由注册顺序说明：/statistics 必须写在 /{task_id} 之前，
否则 statistics 会被当作路径参数解析而返回 422。

接口分为两类：
1. 工作台（Web）使用：创建、批量创建、列表、详情、取消、重试、删除、统计；
2. Electron 客户端使用：认领任务（claim）、上报结果（report）。
   客户端只认领任务并回传结果，登录凭证始终留在客户端本地。
"""

from typing import Optional

from fastapi import APIRouter, Path, Query

from app.api.deps import PublishTaskServiceDep
from app.core.logging import get_logger
from app.models.publish_task import PublishTaskStatus
from app.schemas.common import ApiResponse
from app.schemas.publish_task import (
    PublishTaskBatchCreate,
    PublishTaskClaim,
    PublishTaskCreate,
    PublishTaskListData,
    PublishTaskReport,
    PublishTaskResponse,
)

router = APIRouter(prefix="/publish-tasks", tags=["发布任务"])
logger = get_logger(__name__)


@router.get("/statistics", response_model=ApiResponse[dict], summary="发布任务统计")
def get_statistics(service: PublishTaskServiceDep) -> ApiResponse[dict]:
    """获取发布任务总量与各状态分布，供客户端与工作台展示。"""
    return ApiResponse(data=service.get_statistics())


@router.get("", response_model=ApiResponse[PublishTaskListData], summary="分页查询发布任务")
def list_tasks(
    service: PublishTaskServiceDep,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数，最大 100"),
    status_filter: Optional[str] = Query(
        default=None,
        alias="status",
        description=f"按状态过滤：{'/'.join(PublishTaskStatus.ALL)}",
    ),
    account_id: Optional[int] = Query(default=None, ge=1, description="按账号过滤"),
    content_id: Optional[int] = Query(default=None, ge=1, description="按内容过滤"),
) -> ApiResponse[PublishTaskListData]:
    """分页查询发布任务列表。"""
    items, total = service.list_tasks(
        page=page,
        page_size=page_size,
        status=status_filter,
        account_id=account_id,
        content_id=content_id,
    )
    return ApiResponse(
        data=PublishTaskListData(
            total=total,
            page=page,
            page_size=page_size,
            items=[PublishTaskResponse.from_model(item) for item in items],
        )
    )


@router.post(
    "",
    response_model=ApiResponse[PublishTaskResponse],
    status_code=201,
    summary="创建发布任务",
)
def create_task(
    payload: PublishTaskCreate,
    service: PublishTaskServiceDep,
) -> ApiResponse[PublishTaskResponse]:
    """创建单个发布任务，把一条内容排入某个账号的发布队列。"""
    task = service.create_task(payload)
    return ApiResponse(data=PublishTaskResponse.from_model(task))


@router.post(
    "/batch",
    response_model=ApiResponse[list],
    status_code=201,
    summary="批量创建发布任务",
)
def create_batch(
    payload: PublishTaskBatchCreate,
    service: PublishTaskServiceDep,
) -> ApiResponse[list]:
    """把同一条内容一次分发到多个账号，整批成功或整批失败。"""
    tasks = service.create_batch(payload)
    return ApiResponse(data=[PublishTaskResponse.from_model(task) for task in tasks])


@router.post(
    "/claim",
    response_model=ApiResponse[list],
    summary="客户端认领待执行任务",
)
def claim_tasks(
    payload: PublishTaskClaim,
    service: PublishTaskServiceDep,
) -> ApiResponse[list]:
    """Electron 客户端拉取待执行任务，被认领的任务立即置为发布中。

    并发安全：服务层在同一事务内完成「查询 + 更新」并加行锁，
    同一条任务不会被多个客户端重复认领。
    """
    tasks = service.claim_tasks(payload)
    return ApiResponse(data=[PublishTaskResponse.from_model(task) for task in tasks])


@router.post(
    "/{task_id}/report",
    response_model=ApiResponse[PublishTaskResponse],
    summary="客户端上报发布结果",
)
def report_task(
    payload: PublishTaskReport,
    service: PublishTaskServiceDep,
    task_id: int = Path(..., ge=1, description="任务 ID"),
) -> ApiResponse[PublishTaskResponse]:
    """客户端上报发布结果。

    失败时若未超过重试上限，任务自动回到队列等待再次认领；
    超过上限则置为失败终态。
    """
    task = service.report_task(task_id, payload)
    return ApiResponse(data=PublishTaskResponse.from_model(task))


@router.post(
    "/{task_id}/cancel",
    response_model=ApiResponse[PublishTaskResponse],
    summary="取消发布任务",
)
def cancel_task(
    service: PublishTaskServiceDep,
    task_id: int = Path(..., ge=1, description="任务 ID"),
) -> ApiResponse[PublishTaskResponse]:
    """取消排队中或发布中的任务。"""
    task = service.cancel_task(task_id)
    return ApiResponse(data=PublishTaskResponse.from_model(task))


@router.post(
    "/{task_id}/retry",
    response_model=ApiResponse[PublishTaskResponse],
    summary="重新入队",
)
def retry_task(
    service: PublishTaskServiceDep,
    task_id: int = Path(..., ge=1, description="任务 ID"),
) -> ApiResponse[PublishTaskResponse]:
    """把失败或已取消的任务重新放回队列，并重置重试计数。"""
    task = service.retry_task(task_id)
    return ApiResponse(data=PublishTaskResponse.from_model(task))


@router.get(
    "/{task_id}", response_model=ApiResponse[PublishTaskResponse], summary="获取任务详情"
)
def get_task(
    service: PublishTaskServiceDep,
    task_id: int = Path(..., ge=1, description="任务 ID"),
) -> ApiResponse[PublishTaskResponse]:
    """按 ID 获取发布任务详情。"""
    task = service.get_task(task_id)
    return ApiResponse(data=PublishTaskResponse.from_model(task))


@router.delete(
    "/{task_id}", response_model=ApiResponse[dict], summary="删除发布任务"
)
def delete_task(
    service: PublishTaskServiceDep,
    task_id: int = Path(..., ge=1, description="任务 ID"),
) -> ApiResponse[dict]:
    """删除任务记录，仅允许删除已结束（成功/失败/取消）的任务。"""
    service.delete_task(task_id)
    return ApiResponse(data={"id": task_id})
