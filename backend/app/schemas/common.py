"""通用响应模型。

成功响应统一包装为 {"success": true, "data": ...}，
与全局异常处理器输出的 {"success": false, "error": ...} 保持对称，
前端只需判断 success 字段即可分流。
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

# 响应数据类型变量
T = TypeVar("T")


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
