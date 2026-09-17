"""内容管理接口。

路由注册顺序说明：/statistics 必须写在 /{content_id} 之前，
否则 statistics 会被当作路径参数解析而返回 422。
"""

from typing import Optional

from fastapi import APIRouter, Path, Query

from app.api.deps import ContentServiceDep
from app.core.logging import get_logger
from app.schemas.common import ApiResponse
from app.schemas.content import (
    ContentCreate,
    ContentListData,
    ContentResponse,
    ContentStatistics,
    ContentUpdate,
)

router = APIRouter(prefix="/contents", tags=["内容管理"])
logger = get_logger(__name__)


@router.get("/statistics", response_model=ApiResponse[ContentStatistics], summary="内容统计")
def get_statistics(service: ContentServiceDep) -> ApiResponse[ContentStatistics]:
    """获取内容总量与各状态分布，供工作台首页概览使用。"""
    stats = service.get_statistics()
    return ApiResponse(data=ContentStatistics(**stats))


@router.get("", response_model=ApiResponse[ContentListData], summary="分页查询内容列表")
def list_contents(
    service: ContentServiceDep,
    page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数，最大 100"),
    status_filter: Optional[str] = Query(
        default=None, alias="status", description="按状态过滤：draft/reviewing/published/archived"
    ),
    platform: Optional[str] = Query(default=None, max_length=50, description="按平台过滤"),
    keyword: Optional[str] = Query(
        default=None, max_length=100, description="按标题或正文模糊搜索"
    ),
) -> ApiResponse[ContentListData]:
    """分页查询内容列表，支持状态、平台过滤与关键字搜索。"""
    items, total = service.list_contents(
        page=page,
        page_size=page_size,
        status=status_filter,
        platform=platform,
        keyword=keyword,
    )

    return ApiResponse(
        data=ContentListData(
            total=total,
            page=page,
            page_size=page_size,
            items=[ContentResponse.from_model(item) for item in items],
        )
    )


@router.post(
    "",
    response_model=ApiResponse[ContentResponse],
    status_code=201,
    summary="创建内容",
)
def create_content(
    payload: ContentCreate,
    service: ContentServiceDep,
) -> ApiResponse[ContentResponse]:
    """创建一条新的创作内容。"""
    content = service.create_content(payload)
    return ApiResponse(data=ContentResponse.from_model(content))


@router.get("/{content_id}", response_model=ApiResponse[ContentResponse], summary="获取内容详情")
def get_content(
    service: ContentServiceDep,
    content_id: int = Path(..., ge=1, description="内容 ID"),
) -> ApiResponse[ContentResponse]:
    """按 ID 获取内容详情。"""
    content = service.get_content(content_id)
    return ApiResponse(data=ContentResponse.from_model(content))


@router.put("/{content_id}", response_model=ApiResponse[ContentResponse], summary="更新内容")
def update_content(
    payload: ContentUpdate,
    service: ContentServiceDep,
    content_id: int = Path(..., ge=1, description="内容 ID"),
) -> ApiResponse[ContentResponse]:
    """更新内容，仅更新请求体中显式传入的字段。"""
    content = service.update_content(content_id, payload)
    return ApiResponse(data=ContentResponse.from_model(content))


@router.delete(
    "/{content_id}",
    response_model=ApiResponse[dict],
    summary="删除内容",
)
def delete_content(
    service: ContentServiceDep,
    content_id: int = Path(..., ge=1, description="内容 ID"),
) -> ApiResponse[dict]:
    """删除指定内容。"""
    service.delete_content(content_id)
    return ApiResponse(data={"id": content_id})
