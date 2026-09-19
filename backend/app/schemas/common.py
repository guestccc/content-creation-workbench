"""通用响应模型与序列化工具。

成功响应统一包装为 {"success": true, "data": ...}，
与全局异常处理器输出的 {"success": false, "error": ...} 保持对称，
前端只需判断 success 字段即可分流。
"""

import os
from datetime import datetime, timezone
from typing import Annotated, Generic, List, TypeVar

from pydantic import BaseModel, Field, field_serializer, field_validator

# 响应数据类型变量
T = TypeVar("T")


def absolutize_path(value: str, field: str) -> str:
    """展开 ~ 并规范化为绝对路径；相对路径直接判非法。

    后端的 cwd 与用户敲命令时的 cwd 不是一回事，相对路径会指向一个用户
    完全没预期的地方，所以宁可 422 也不猜。

    用 os.path.abspath 而不是 Path.resolve()：前者只做词法规范化，
    不会去解析符号链接（macOS 上 /tmp 会被 resolve 成 /private/tmp，
    会让用户看到自己没输入过的路径）。

    Raises:
        ValueError: 值为空或不是绝对路径（Pydantic 会转成 422 响应）。
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} 不能为空")
    expanded = os.path.expanduser(cleaned)
    if not os.path.isabs(expanded):
        raise ValueError(f"{field} 必须是绝对路径（以 / 开头），当前为：{cleaned}")
    return os.path.abspath(expanded)


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


# 单次批量删除的最大任务数：历史列表一页 10 条，100 已远超「全选一页」
MAX_BATCH_DELETE_SIZE = 100


class JobBatchDeleteRequest(BaseModel):
    """批量删除任务记录：整批成功或整批失败。

    五个任务域（镜头分割 / 字幕提取 / 混剪 / 素材抓取 / 一键成品）共用一个模型 ——
    内容没有任何领域差异，不做五份逐字拷贝。
    """

    ids: List[Annotated[int, Field(ge=1)]] = Field(
        ...,
        min_length=1,
        max_length=MAX_BATCH_DELETE_SIZE,
        description="待删除的任务 ID 列表",
    )
    purge_files: bool = Field(
        default=False,
        description="是否连同磁盘上的任务产物一起删除（默认保留，只删记录）",
    )

    @field_validator("ids")
    @classmethod
    def _dedupe(cls, value: List[int]) -> List[int]:
        # 保序去重：重复勾选不该让 count 虚高
        return list(dict.fromkeys(value))


class HealthData(BaseModel):
    """健康检查返回数据。"""

    status: str = Field(description="整体状态：ok / degraded")
    app_name: str = Field(description="应用名称")
    version: str = Field(description="应用版本")
    database: str = Field(description="数据库状态：connected / disconnected")
