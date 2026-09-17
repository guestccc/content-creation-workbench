"""平台账号管理接口。

安全约定：本模块不接收、不返回任何登录凭证。
平台 Cookie / Token 由 Electron 客户端使用 safeStorage 加密后保存在本地，
后端只保存账号的元信息（平台、昵称、状态、备注）。
"""

from typing import Optional

from fastapi import APIRouter, Path, Query

from app.api.deps import AccountServiceDep
from app.core.logging import get_logger
from app.models.account import AccountStatus
from app.schemas.account import (
    AccountCreate,
    AccountListData,
    AccountResponse,
    AccountUpdate,
)
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/accounts", tags=["账号管理"])
logger = get_logger(__name__)


@router.get("", response_model=ApiResponse[AccountListData], summary="查询账号列表")
def list_accounts(
    service: AccountServiceDep,
    platform: Optional[str] = Query(default=None, max_length=50, description="按平台过滤"),
    status_filter: Optional[str] = Query(
        default=None,
        alias="status",
        description=f"按状态过滤：{'/'.join(AccountStatus.ALL)}",
    ),
    keyword: Optional[str] = Query(
        default=None, max_length=100, description="按昵称或备注模糊搜索"
    ),
) -> ApiResponse[AccountListData]:
    """查询账号列表。

    账号数量通常有限，这里一次性返回全部结果，不做分页。
    """
    items, total = service.list_accounts(
        platform=platform, status=status_filter, keyword=keyword
    )
    return ApiResponse(
        data=AccountListData(
            total=total,
            items=[AccountResponse.from_model(item) for item in items],
        )
    )


@router.post(
    "",
    response_model=ApiResponse[AccountResponse],
    status_code=201,
    summary="创建账号",
)
def create_account(
    payload: AccountCreate,
    service: AccountServiceDep,
) -> ApiResponse[AccountResponse]:
    """创建平台账号记录（不含凭证）。"""
    account = service.create_account(payload)
    return ApiResponse(data=AccountResponse.from_model(account))


@router.get(
    "/{account_id}", response_model=ApiResponse[AccountResponse], summary="获取账号详情"
)
def get_account(
    service: AccountServiceDep,
    account_id: int = Path(..., ge=1, description="账号 ID"),
) -> ApiResponse[AccountResponse]:
    """按 ID 获取账号详情。"""
    account = service.get_account(account_id)
    return ApiResponse(data=AccountResponse.from_model(account))


@router.put(
    "/{account_id}", response_model=ApiResponse[AccountResponse], summary="更新账号"
)
def update_account(
    payload: AccountUpdate,
    service: AccountServiceDep,
    account_id: int = Path(..., ge=1, description="账号 ID"),
) -> ApiResponse[AccountResponse]:
    """更新账号，仅更新请求体中显式传入的字段。"""
    account = service.update_account(account_id, payload)
    return ApiResponse(data=AccountResponse.from_model(account))


@router.delete(
    "/{account_id}", response_model=ApiResponse[dict], summary="删除账号"
)
def delete_account(
    service: AccountServiceDep,
    account_id: int = Path(..., ge=1, description="账号 ID"),
) -> ApiResponse[dict]:
    """删除账号。账号下仍有未完成的发布任务时会被拒绝。"""
    service.delete_account(account_id)
    return ApiResponse(data={"id": account_id})
