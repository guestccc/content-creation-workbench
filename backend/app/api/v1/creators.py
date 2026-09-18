"""创作者主页管理接口。

素材抓取「创作者主页」模式的素材库：按平台维护常抓的创作者，
抓取页从这里选人，免去每次手工粘链接。
"""

from typing import Optional

from fastapi import APIRouter, Path, Query

from app.api.deps import CreatorServiceDep
from app.core.logging import get_logger
from app.models.crawl_job import CrawlPlatform
from app.schemas.common import ApiResponse
from app.schemas.creator import (
    CreatorCreate,
    CreatorListData,
    CreatorResponse,
    CreatorUpdate,
)

router = APIRouter(prefix="/creators", tags=["创作者主页"])
logger = get_logger(__name__)


@router.get("", response_model=ApiResponse[CreatorListData], summary="查询创作者列表")
def list_creators(
    service: CreatorServiceDep,
    platform: Optional[str] = Query(
        default=None, max_length=20, description=f"按平台过滤：{'/'.join(CrawlPlatform.ALL)}"
    ),
    tag: Optional[str] = Query(
        default=None, max_length=20, description="按标签过滤（精确匹配单个标签）"
    ),
) -> ApiResponse[CreatorListData]:
    """查询创作者列表。

    创作者数量通常有限，一次性返回全部结果，不做分页；
    平台维度的区分由前端按 platform 字段分组展示。
    """
    items, total = service.list_creators(platform=platform, tag=tag)
    return ApiResponse(
        data=CreatorListData(
            total=total,
            items=[CreatorResponse.from_model(item) for item in items],
        )
    )


@router.post(
    "",
    response_model=ApiResponse[CreatorResponse],
    status_code=201,
    summary="创建创作者",
)
def create_creator(
    payload: CreatorCreate,
    service: CreatorServiceDep,
) -> ApiResponse[CreatorResponse]:
    """新增一位创作者（同平台下主页重复会返回 409）。"""
    creator = service.create_creator(payload)
    return ApiResponse(data=CreatorResponse.from_model(creator))


@router.get(
    "/{creator_id}", response_model=ApiResponse[CreatorResponse], summary="获取创作者详情"
)
def get_creator(
    service: CreatorServiceDep,
    creator_id: int = Path(..., ge=1, description="创作者 ID"),
) -> ApiResponse[CreatorResponse]:
    """按 ID 获取创作者详情。"""
    creator = service.get_creator(creator_id)
    return ApiResponse(data=CreatorResponse.from_model(creator))


@router.put(
    "/{creator_id}", response_model=ApiResponse[CreatorResponse], summary="更新创作者"
)
def update_creator(
    payload: CreatorUpdate,
    service: CreatorServiceDep,
    creator_id: int = Path(..., ge=1, description="创作者 ID"),
) -> ApiResponse[CreatorResponse]:
    """更新创作者，仅更新请求体中显式传入的字段。"""
    creator = service.update_creator(creator_id, payload)
    return ApiResponse(data=CreatorResponse.from_model(creator))


@router.delete(
    "/{creator_id}", response_model=ApiResponse[dict], summary="删除创作者"
)
def delete_creator(
    service: CreatorServiceDep,
    creator_id: int = Path(..., ge=1, description="创作者 ID"),
) -> ApiResponse[dict]:
    """删除创作者记录（历史抓取任务的参数快照不受影响）。"""
    service.delete_creator(creator_id)
    return ApiResponse(data={"id": creator_id})
