"""平台账号相关的 Pydantic 模型。

注意：请求与响应模型中**不包含任何凭证字段**。
登录凭证由 Electron 客户端本地加密保存，不经过后端。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.models.account import Account, AccountStatus
from app.schemas.common import TimestampMixin


def _validate_account_status(value: str) -> str:
    """校验账号状态取值合法。"""
    if value not in AccountStatus.ALL:
        raise ValueError(f"status 必须是 {list(AccountStatus.ALL)} 之一")
    return value


class AccountCreate(BaseModel):
    """创建账号的请求体。"""

    platform: str = Field(..., min_length=1, max_length=50, description="所属平台")
    nickname: str = Field(..., min_length=1, max_length=100, description="账号昵称")
    account_uid: str = Field(default="", max_length=100, description="平台侧账号 ID，可留空")
    status: str = Field(default=AccountStatus.ACTIVE, description="账号状态")
    remark: str = Field(default="", max_length=500, description="备注")

    @field_validator("platform", "nickname")
    @classmethod
    def _check_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("该字段不能为空")
        return cleaned

    @field_validator("account_uid", "remark")
    @classmethod
    def _strip_optional_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: str) -> str:
        return _validate_account_status(value)


class AccountUpdate(BaseModel):
    """更新账号的请求体，所有字段可选。"""

    platform: Optional[str] = Field(default=None, min_length=1, max_length=50)
    nickname: Optional[str] = Field(default=None, min_length=1, max_length=100)
    account_uid: Optional[str] = Field(default=None, max_length=100)
    status: Optional[str] = Field(default=None)
    remark: Optional[str] = Field(default=None, max_length=500)

    @field_validator("platform", "nickname")
    @classmethod
    def _check_required_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("该字段不能为空")
        return cleaned

    @field_validator("account_uid", "remark")
    @classmethod
    def _strip_optional_text(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value is not None else value

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: Optional[str]) -> Optional[str]:
        return _validate_account_status(value) if value is not None else None


class AccountResponse(TimestampMixin):
    """账号详情响应。"""

    id: int = Field(description="主键")
    platform: str = Field(description="所属平台")
    nickname: str = Field(description="账号昵称")
    account_uid: str = Field(description="平台侧账号 ID")
    status: str = Field(description="账号状态")
    remark: str = Field(description="备注")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    @classmethod
    def from_model(cls, model: Account) -> "AccountResponse":
        """由 ORM 对象构造响应模型。"""
        return cls(
            id=model.id,
            platform=model.platform,
            nickname=model.nickname,
            account_uid=model.account_uid,
            status=model.status,
            remark=model.remark,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class AccountListData(BaseModel):
    """账号列表数据。"""

    total: int = Field(description="总条数")
    items: List[AccountResponse] = Field(description="账号列表")
