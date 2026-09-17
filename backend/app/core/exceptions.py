"""业务异常定义。

设计原则：
1. 所有业务异常继承 AppException，由全局异常处理器统一转换为标准响应体；
2. 异常自带 code / status_code，业务代码无需关心 HTTP 细节；
3. 未预期的异常由兜底处理器捕获，绝不向前端泄露堆栈信息。
"""

from typing import Any, Optional


class AppException(Exception):
    """业务异常基类。"""

    code: str = "APP_ERROR"
    message: str = "服务处理异常"
    status_code: int = 400

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        code: Optional[str] = None,
        status_code: Optional[int] = None,
        details: Optional[Any] = None,
    ) -> None:
        """构造业务异常。

        Args:
            message: 面向用户的错误描述，省略时使用类默认值。
            code: 业务错误码，省略时使用类默认值。
            status_code: HTTP 状态码，省略时使用类默认值。
            details: 附加信息（如字段级校验明细），会原样返回给前端。
        """
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details
        super().__init__(self.message)


class BadRequestError(AppException):
    """请求参数不合法。"""

    code = "BAD_REQUEST"
    message = "请求参数不合法"
    status_code = 400


class NotFoundError(AppException):
    """资源不存在。"""

    code = "NOT_FOUND"
    message = "资源不存在"
    status_code = 404


class ConflictError(AppException):
    """资源冲突，例如唯一约束重复。"""

    code = "CONFLICT"
    message = "资源已存在"
    status_code = 409


class DatabaseError(AppException):
    """数据库操作失败。"""

    code = "DATABASE_ERROR"
    message = "数据库操作失败"
    status_code = 500
