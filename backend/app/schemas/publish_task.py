"""发布任务相关的 Pydantic 模型。

职责边界：
- 后端只负责任务排队与状态流转；
- 实际的发布动作由 Electron 客户端「认领」后执行，再回调上报结果；
- 因此这里既包含面向 Web 端的创建接口模型，也包含面向客户端的认领/上报模型。
"""

from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer, field_validator

from app.models.publish_task import PublishTask
from app.schemas.common import TimestampMixin, to_utc_iso

# 单次批量创建的最大任务数，避免误操作瞬间产生海量任务
MAX_BATCH_SIZE = 50


def _to_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """把带时区的时间统一转换为 naive UTC，与数据库存储格式保持一致。

    前端可能传入 "2026-09-18T10:00:00Z" 这类带时区的时间，
    也有可能是 "2026-09-18T10:00:00" 这类不含时区的本地时间。
    前者按 UTC 换算，后者直接按 UTC 解释。
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class PublishTaskCreate(BaseModel):
    """创建单个发布任务。"""

    content_id: int = Field(..., ge=1, description="待发布的内容 ID")
    account_id: int = Field(..., ge=1, description="目标账号 ID")
    scheduled_at: Optional[datetime] = Field(
        default=None, description="计划执行时间，留空表示尽快执行"
    )
    max_retries: int = Field(default=2, ge=0, le=10, description="最大重试次数")

    @field_validator("scheduled_at")
    @classmethod
    def _normalize_scheduled(cls, value: Optional[datetime]) -> Optional[datetime]:
        return _to_naive_utc(value)


class PublishTaskBatchCreate(BaseModel):
    """批量创建发布任务：把同一条内容分发到多个账号。"""

    content_id: int = Field(..., ge=1, description="待发布的内容 ID")
    account_ids: List[int] = Field(
        ..., min_length=1, max_length=MAX_BATCH_SIZE, description="目标账号 ID 列表"
    )
    scheduled_at: Optional[datetime] = Field(
        default=None, description="计划执行时间，留空表示尽快执行"
    )
    max_retries: int = Field(default=2, ge=0, le=10, description="最大重试次数")

    @field_validator("scheduled_at")
    @classmethod
    def _normalize_scheduled(cls, value: Optional[datetime]) -> Optional[datetime]:
        return _to_naive_utc(value)

    @field_validator("account_ids")
    @classmethod
    def _dedupe_accounts(cls, value: List[int]) -> List[int]:
        """去重并保持原顺序，避免同一账号被重复创建任务。"""
        seen: set = set()
        result: List[int] = []
        for account_id in value:
            if account_id not in seen:
                seen.add(account_id)
                result.append(account_id)
        return result


class PublishTaskClaim(BaseModel):
    """客户端认领任务的请求体。"""

    limit: int = Field(default=5, ge=1, le=20, description="本次最多认领的任务数")
    account_id: Optional[int] = Field(
        default=None, ge=1, description="仅认领指定账号的任务，留空表示不限"
    )


class PublishTaskReport(BaseModel):
    """客户端上报的发布结果。"""

    success: bool = Field(..., description="是否发布成功")
    result_url: str = Field(default="", max_length=1000, description="发布成功后的内容链接")
    error_message: str = Field(default="", max_length=2000, description="失败原因")

    @field_validator("result_url", "error_message")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class PublishTaskResponse(TimestampMixin):
    """发布任务详情响应。"""

    id: int = Field(description="主键")
    content_id: int = Field(description="内容 ID")
    content_title: str = Field(description="内容标题（冗余字段，便于列表展示）")
    account_id: int = Field(description="账号 ID")
    account_nickname: str = Field(description="账号昵称（冗余字段，便于列表展示）")
    platform: str = Field(description="目标平台")
    status: str = Field(description="任务状态")
    scheduled_at: Optional[datetime] = Field(default=None, description="计划执行时间")
    started_at: Optional[datetime] = Field(default=None, description="开始时间")
    finished_at: Optional[datetime] = Field(default=None, description="结束时间")
    retry_count: int = Field(description="已重试次数")
    max_retries: int = Field(description="最大重试次数")
    error_message: str = Field(description="失败原因")
    result_url: str = Field(description="发布成功后的内容链接")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")

    @field_serializer("scheduled_at", "started_at", "finished_at")
    def _serialize_optional_times(self, value: Optional[datetime]) -> Optional[str]:
        return to_utc_iso(value) if value is not None else None

    @classmethod
    def from_model(cls, model: PublishTask) -> "PublishTaskResponse":
        """由 ORM 对象构造响应模型。"""
        return cls(
            id=model.id,
            content_id=model.content_id,
            content_title=model.content.title if model.content else "",
            account_id=model.account_id,
            account_nickname=model.account.nickname if model.account else "",
            platform=model.account.platform if model.account else "",
            status=model.status,
            scheduled_at=model.scheduled_at,
            started_at=model.started_at,
            finished_at=model.finished_at,
            retry_count=model.retry_count,
            max_retries=model.max_retries,
            error_message=model.error_message,
            result_url=model.result_url,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class PublishTaskListData(BaseModel):
    """发布任务列表数据。"""

    total: int = Field(description="满足条件的总条数")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页条数")
    items: List[PublishTaskResponse] = Field(description="当前页数据")
