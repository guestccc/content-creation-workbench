"""通用响应模型与序列化工具。

成功响应统一包装为 {"success": true, "data": ...}，
与全局异常处理器输出的 {"success": false, "error": ...} 保持对称，
前端只需判断 success 字段即可分流。
"""

from datetime import datetime, timezone
from typing import Generic, TypeVar

from pydantic import BaseModel, Field, field_serializer

# 响应数据类型变量
T = TypeVar("T")


def to_utc_iso(value: datetime) -> str:
    """把 naive UTC 时间序列化为带 Z 后缀的 ISO8601 字符串。

    数据库存储的是 naive UTC（SQLite 不保存时区），若直接输出，
    浏览器会按本地时区解析导致时间偏移。这里显式补上 UTC 标识，
    前端 new Date() 即可解析出正确的本地时间。
    """
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


class TimestampMixin(BaseModel):
    """为响应模型提供统一的 created_at / updated_at 序列化。

    使用 check_fields=False，允许子类不含同名字段时也能复用本 mixin。
    """

    @field_serializer("created_at", "updated_at", check_fields=False)
    def _serialize_timestamps(self, value: datetime) -> str:
        return to_utc_iso(value)


class ApiResponse(BaseModel, Generic[T]):
    """统一成功响应包装。"""

    success: bool = Field(default=True, description="请求是否成功")
    data: T = Field(description="业务数据")


class HealthData(BaseModel):
    """健康检查返回数据。"""

    status: str = Field(description="整体状态：ok / degraded")
    app_name: str = Field(description="应用名称")
    version: str = Field(description="应用版本")
    database: str = Field(description="数据库状态：connected / disconnected")
