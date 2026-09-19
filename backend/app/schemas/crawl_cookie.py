"""Cookie 库相关的 Pydantic 模型。

platform 取值复用素材抓取的 CrawlPlatform 常量 —— Cookie 库本来就是
抓取功能的凭据库，平台集合应当与抓取支持的平台严格一致。

安全约定：cookie 字段是凭据，只在「创建/更新请求」与「详情响应」中出现；
列表响应不返回 cookie 原文（用 cookie_preview 截断展示）。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.models.crawl_cookie import CrawlCookie
from app.models.crawl_job import CrawlPlatform
from app.schemas.common import TimestampMixin


def _validate_platform(value: str) -> str:
    """校验平台取值合法（与素材抓取支持的平台一致）。"""
    cleaned = value.strip().lower()
    if cleaned not in CrawlPlatform.ALL:
        raise ValueError(f"不支持的平台：{value}（可选：{', '.join(CrawlPlatform.ALL)}）")
    return cleaned


class CrawlCookieCreate(BaseModel):
    """创建 Cookie 的请求体。"""

    platform: str = Field(..., max_length=20, description=f"平台：{'/'.join(CrawlPlatform.ALL)}")
    name: str = Field(..., min_length=1, max_length=50, description="展示名（如「主号」「小号」）")
    cookie: str = Field(..., min_length=1, max_length=8000, description="登录 Cookie 串")
    remark: str = Field(default="", max_length=200, description="备注")

    @field_validator("platform")
    @classmethod
    def _check_platform(cls, value: str) -> str:
        return _validate_platform(value)

    @field_validator("name", "cookie")
    @classmethod
    def _check_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("该字段不能为空")
        return cleaned

    @field_validator("remark")
    @classmethod
    def _strip_optional_text(cls, value: str) -> str:
        return value.strip()


class CrawlCookieUpdate(BaseModel):
    """更新 Cookie 的请求体，所有字段可选。"""

    platform: Optional[str] = Field(default=None, max_length=20)
    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    cookie: Optional[str] = Field(default=None, min_length=1, max_length=8000)
    remark: Optional[str] = Field(default=None, max_length=200)

    @field_validator("platform")
    @classmethod
    def _check_platform(cls, value: Optional[str]) -> Optional[str]:
        return _validate_platform(value) if value is not None else None

    @field_validator("name", "cookie")
    @classmethod
    def _check_required_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("该字段不能为空")
        return cleaned

    @field_validator("remark")
    @classmethod
    def _strip_optional_text(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value is not None else value


class CrawlCookieResponse(TimestampMixin):
    """Cookie 详情响应（含完整 cookie 串，抓取时透传用）。"""

    id: int = Field(description="主键")
    platform: str = Field(description="平台标识")
    platform_label: str = Field(description="平台中文名")
    name: str = Field(description="展示名")
    cookie: str = Field(description="完整 Cookie 串")
    remark: str = Field(description="备注")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    @classmethod
    def from_model(cls, model: CrawlCookie) -> "CrawlCookieResponse":
        """由 ORM 对象构造响应模型。"""
        return cls(
            id=model.id,
            platform=model.platform,
            platform_label=CrawlPlatform.LABELS.get(model.platform, model.platform),
            name=model.name,
            cookie=model.cookie,
            remark=model.remark,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class CrawlCookieListItem(TimestampMixin):
    """Cookie 列表项（不含完整 cookie 串，只有截断预览）。"""

    id: int = Field(description="主键")
    platform: str = Field(description="平台标识")
    platform_label: str = Field(description="平台中文名")
    name: str = Field(description="展示名")
    cookie_preview: str = Field(description="Cookie 截断预览（前 60 字符）")
    remark: str = Field(description="备注")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    @classmethod
    def from_model(cls, model: CrawlCookie) -> "CrawlCookieListItem":
        """由 ORM 对象构造列表项（cookie 只给预览，不给原文）。"""
        return cls(
            id=model.id,
            platform=model.platform,
            platform_label=CrawlPlatform.LABELS.get(model.platform, model.platform),
            name=model.name,
            cookie_preview=model.cookie[:60] + ("…" if len(model.cookie) > 60 else ""),
            remark=model.remark,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class CrawlCookieListData(BaseModel):
    """Cookie 列表数据（量少不分页，一次返回全部）。"""

    total: int = Field(description="总条数")
    items: List[CrawlCookieListItem] = Field(description="Cookie 列表")
