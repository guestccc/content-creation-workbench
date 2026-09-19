"""Cookie 库管理接口。

素材抓取「Cookie 登录」的凭据库：按平台保存常用的登录 Cookie，
抓取页从这里选取，免去每次手工粘贴。

列表响应只给 cookie_preview（截断预览），完整 cookie 串只在详情响应里。
"""

from typing import Optional

from fastapi import APIRouter, Path, Query

from app.api.deps import CrawlCookieServiceDep
from app.core.logging import get_logger
from app.models.crawl_job import CrawlPlatform
from app.schemas.common import ApiResponse
from app.schemas.crawl_cookie import (
    CrawlCookieCreate,
    CrawlCookieListData,
    CrawlCookieListItem,
    CrawlCookieResponse,
    CrawlCookieUpdate,
)

router = APIRouter(prefix="/crawl-cookies", tags=["Cookie 库"])
logger = get_logger(__name__)


@router.get("", response_model=ApiResponse[CrawlCookieListData], summary="查询 Cookie 列表")
def list_cookies(
    service: CrawlCookieServiceDep,
    platform: Optional[str] = Query(
        default=None, max_length=20, description=f"按平台过滤：{'/'.join(CrawlPlatform.ALL)}"
    ),
) -> ApiResponse[CrawlCookieListData]:
    """查询 Cookie 列表（量少不分页）。

    列表项只含 cookie_preview（截断预览），完整串走详情接口拿。
    """
    items, total = service.list_cookies(platform=platform)
    return ApiResponse(
        data=CrawlCookieListData(
            total=total,
            items=[CrawlCookieListItem.from_model(item) for item in items],
        )
    )


@router.post(
    "",
    response_model=ApiResponse[CrawlCookieResponse],
    status_code=201,
    summary="保存 Cookie",
)
def create_cookie(
    payload: CrawlCookieCreate,
    service: CrawlCookieServiceDep,
) -> ApiResponse[CrawlCookieResponse]:
    """保存一条 Cookie（同平台下名称重复会返回 409）。"""
    cookie = service.create_cookie(payload)
    return ApiResponse(data=CrawlCookieResponse.from_model(cookie))


@router.get(
    "/{cookie_id}",
    response_model=ApiResponse[CrawlCookieResponse],
    summary="获取 Cookie 详情",
)
def get_cookie(
    service: CrawlCookieServiceDep,
    cookie_id: int = Path(..., ge=1, description="Cookie ID"),
) -> ApiResponse[CrawlCookieResponse]:
    """按 ID 获取 Cookie 完整内容（抓取页选中后用它回填表单）。"""
    cookie = service.get_cookie(cookie_id)
    return ApiResponse(data=CrawlCookieResponse.from_model(cookie))


@router.put(
    "/{cookie_id}",
    response_model=ApiResponse[CrawlCookieResponse],
    summary="更新 Cookie",
)
def update_cookie(
    payload: CrawlCookieUpdate,
    service: CrawlCookieServiceDep,
    cookie_id: int = Path(..., ge=1, description="Cookie ID"),
) -> ApiResponse[CrawlCookieResponse]:
    """更新 Cookie，仅更新请求体中显式传入的字段。"""
    cookie = service.update_cookie(cookie_id, payload)
    return ApiResponse(data=CrawlCookieResponse.from_model(cookie))


@router.delete(
    "/{cookie_id}",
    response_model=ApiResponse[dict],
    summary="删除 Cookie",
)
def delete_cookie(
    service: CrawlCookieServiceDep,
    cookie_id: int = Path(..., ge=1, description="Cookie ID"),
) -> ApiResponse[dict]:
    """删除 Cookie 记录（历史抓取任务不受影响）。"""
    service.delete_cookie(cookie_id)
    return ApiResponse(data={"id": cookie_id})
