"""创作者主页相关的 Pydantic 模型。

platform 取值复用素材抓取的 CrawlPlatform 常量 —— 创作者库本来就是
抓取功能的素材库，平台集合应当与抓取支持的平台严格一致。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.models.crawl_job import CrawlPlatform
from app.models.creator import Creator
from app.schemas.common import TimestampMixin

# 标签上限：防误粘贴整篇文章进来；标签多了筛选也就失去意义了
MAX_TAGS = 10
MAX_TAG_LENGTH = 20


def _validate_platform(value: str) -> str:
    """校验平台取值合法（与素材抓取支持的平台一致）。"""
    cleaned = value.strip().lower()
    if cleaned not in CrawlPlatform.ALL:
        raise ValueError(f"不支持的平台：{value}（可选：{', '.join(CrawlPlatform.ALL)}）")
    return cleaned


def _clean_tags(value: Optional[List[str]]) -> List[str]:
    """标签清洗：trim、去空项、超长报错、去重保序。"""
    if value is None:
        return []
    cleaned: List[str] = []
    for item in value:
        text = item.strip()
        if not text:
            continue
        if len(text) > MAX_TAG_LENGTH:
            raise ValueError(f"单个标签不能超过 {MAX_TAG_LENGTH} 个字：{text}")
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > MAX_TAGS:
        raise ValueError(f"标签最多 {MAX_TAGS} 个")
    return cleaned


class CreatorCreate(BaseModel):
    """创建创作者的请求体。"""

    platform: str = Field(..., max_length=20, description=f"平台：{'/'.join(CrawlPlatform.ALL)}")
    name: str = Field(..., min_length=1, max_length=50, description="展示名（博主昵称或自起名）")
    homepage: str = Field(
        ..., min_length=1, max_length=500, description="主页链接或 ID（抓取时透传给 MC）"
    )
    tags: List[str] = Field(default_factory=list, description="标签列表")
    remark: str = Field(default="", max_length=200, description="备注")

    @field_validator("platform")
    @classmethod
    def _check_platform(cls, value: str) -> str:
        return _validate_platform(value)

    @field_validator("name", "homepage")
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

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, value: List[str]) -> List[str]:
        return _clean_tags(value)


class CreatorUpdate(BaseModel):
    """更新创作者的请求体，所有字段可选。"""

    platform: Optional[str] = Field(default=None, max_length=20)
    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    homepage: Optional[str] = Field(default=None, min_length=1, max_length=500)
    tags: Optional[List[str]] = Field(default=None)
    remark: Optional[str] = Field(default=None, max_length=200)

    @field_validator("platform")
    @classmethod
    def _check_platform(cls, value: Optional[str]) -> Optional[str]:
        return _validate_platform(value) if value is not None else None

    @field_validator("name", "homepage")
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

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return _clean_tags(value) if value is not None else None


class CreatorResponse(TimestampMixin):
    """创作者详情响应。"""

    id: int = Field(description="主键")
    platform: str = Field(description="平台标识")
    platform_label: str = Field(description="平台中文名")
    name: str = Field(description="展示名")
    homepage: str = Field(description="主页链接或 ID")
    tags: List[str] = Field(default_factory=list, description="标签列表")
    remark: str = Field(description="备注")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    @classmethod
    def from_model(cls, model: Creator) -> "CreatorResponse":
        """由 ORM 对象构造响应模型。"""
        return cls(
            id=model.id,
            platform=model.platform,
            platform_label=CrawlPlatform.LABELS.get(model.platform, model.platform),
            name=model.name,
            homepage=model.homepage,
            tags=list(model.tags or []),
            remark=model.remark,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class CreatorListData(BaseModel):
    """创作者列表数据（量少不分页，一次返回全部）。"""

    total: int = Field(description="总条数")
    items: List[CreatorResponse] = Field(description="创作者列表")
