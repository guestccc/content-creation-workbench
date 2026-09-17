"""内容相关的 Pydantic 请求/响应模型。

分层约定：
- ContentCreate / ContentUpdate：请求体，负责入参校验；
- ContentResponse：响应体，负责把 ORM 对象转换为对前端友好的结构；
- ContentListData：分页列表数据。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.models.content import Content, ContentStatus
from app.schemas.common import TimestampMixin

# 单个标签的最大长度
MAX_TAG_LENGTH = 30
# 单条内容最多允许的标签数
MAX_TAG_COUNT = 10


def _normalize_tags(tags: List[str]) -> List[str]:
    """标签统一处理：去空白、丢弃空值、去重、校验长度与数量。"""
    result: List[str] = []
    seen: set = set()
    for tag in tags:
        cleaned = tag.strip()
        if not cleaned:
            continue
        if len(cleaned) > MAX_TAG_LENGTH:
            raise ValueError(f"单个标签长度不能超过 {MAX_TAG_LENGTH} 个字符：{cleaned}")
        if cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    if len(result) > MAX_TAG_COUNT:
        raise ValueError(f"标签数量不能超过 {MAX_TAG_COUNT} 个")
    return result


def _validate_status(value: str) -> str:
    """校验内容状态取值合法。"""
    if value not in ContentStatus.ALL:
        raise ValueError(f"status 必须是 {list(ContentStatus.ALL)} 之一")
    return value


class ContentCreate(BaseModel):
    """创建内容的请求体。"""

    title: str = Field(..., min_length=1, max_length=200, description="标题")
    body: str = Field(default="", max_length=50000, description="正文")
    platform: str = Field(default="", max_length=50, description="目标平台")
    status: str = Field(default=ContentStatus.DRAFT, description="内容状态")
    tags: List[str] = Field(default_factory=list, description="标签列表")
    author: str = Field(default="", max_length=100, description="作者")

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("标题不能为空")
        return cleaned

    @field_validator("platform", "author")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: str) -> str:
        return _validate_status(value)

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, value: List[str]) -> List[str]:
        return _normalize_tags(value)


class ContentUpdate(BaseModel):
    """更新内容的请求体。

    所有字段均为可选：未传入的字段保持原值，传 null 的字段会被忽略。
    """

    title: Optional[str] = Field(default=None, min_length=1, max_length=200, description="标题")
    body: Optional[str] = Field(default=None, max_length=50000, description="正文")
    platform: Optional[str] = Field(default=None, max_length=50, description="目标平台")
    status: Optional[str] = Field(default=None, description="内容状态")
    tags: Optional[List[str]] = Field(default=None, description="标签列表")
    author: Optional[str] = Field(default=None, max_length=100, description="作者")

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("标题不能为空")
        return cleaned

    @field_validator("platform", "author")
    @classmethod
    def _strip_text(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value is not None else value

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: Optional[str]) -> Optional[str]:
        return _validate_status(value) if value is not None else value

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return _normalize_tags(value) if value is not None else value


class ContentResponse(TimestampMixin):
    """内容详情响应。"""

    id: int = Field(description="主键")
    title: str = Field(description="标题")
    body: str = Field(description="正文")
    platform: str = Field(description="目标平台")
    status: str = Field(description="内容状态")
    tags: List[str] = Field(description="标签列表")
    author: str = Field(description="作者")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    @classmethod
    def from_model(cls, model: Content) -> "ContentResponse":
        """由 ORM 对象构造响应模型（tags 需从逗号字符串还原为列表）。"""
        return cls(
            id=model.id,
            title=model.title,
            body=model.body,
            platform=model.platform,
            status=model.status,
            tags=model.tag_list,
            author=model.author,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class ContentListData(BaseModel):
    """分页列表数据。"""

    total: int = Field(description="满足条件的总条数")
    page: int = Field(description="当前页码，从 1 开始")
    page_size: int = Field(description="每页条数")
    items: List[ContentResponse] = Field(description="当前页数据")


class ContentStatistics(BaseModel):
    """内容统计信息，供工作台首页概览使用。"""

    total: int = Field(description="内容总数")
    by_status: dict = Field(description="按状态分组的数量统计")
