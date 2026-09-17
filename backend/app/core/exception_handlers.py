"""全局异常处理器。

保证任何异常都返回统一 JSON 结构，且服务端异常不会把堆栈信息暴露给客户端。

错误响应体结构：
{
    "success": false,
    "error": {"code": "NOT_FOUND", "message": "内容不存在", "details": null}
}
"""

from typing import Any, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppException
from app.core.logging import get_logger

logger = get_logger(__name__)

# 422 状态码常量：新版 starlette 把 HTTP_422_UNPROCESSABLE_ENTITY 重命名为
# HTTP_422_UNPROCESSABLE_CONTENT，这里做兼容处理，避免不同版本下报错。
UNPROCESSABLE_STATUS: int = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)

# 校验错误的 loc 首段是参数来源（body/query/path...），
# 对前端没有意义，输出时去掉，只保留真实字段路径。
_LOCATION_PREFIXES = frozenset({"body", "query", "path", "header", "cookie"})


def _format_field(loc: tuple) -> str:
    """把 Pydantic 的 loc 元组格式化为字段路径，如 ('body', 'title') -> 'title'。"""
    parts = [str(part) for part in loc]
    if parts and parts[0] in _LOCATION_PREFIXES:
        parts = parts[1:]
    return ".".join(parts) if parts else "request"


def _error_body(code: str, message: str, details: Optional[Any] = None) -> dict:
    """构造统一的错误响应体。"""
    error: dict = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"success": False, "error": error}


def register_exception_handlers(app: FastAPI) -> None:
    """把所有异常处理器注册到应用上。

    Args:
        app: FastAPI 应用实例。
    """

    @app.exception_handler(AppException)
    async def _handle_app_exception(request: Request, exc: AppException) -> JSONResponse:
        """业务异常：按异常自带的状态码与错误码返回。"""
        logger.warning(
            "业务异常 | path=%s | code=%s | message=%s",
            request.url.path,
            exc.code,
            exc.message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """请求体 / 查询参数校验失败，转为字段级明细返回。"""
        errors = [
            {
                "field": _format_field(err.get("loc", ())),
                "reason": err.get("msg", ""),
            }
            for err in exc.errors()
        ]
        logger.warning("参数校验失败 | path=%s | errors=%s", request.url.path, errors)
        return JSONResponse(
            status_code=UNPROCESSABLE_STATUS,
            content=_error_body("VALIDATION_ERROR", "请求参数校验失败", errors),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """HTTP 异常，如路由不存在（404）、方法不允许（405）。"""
        logger.warning(
            "HTTP 异常 | path=%s | status=%s | detail=%s",
            request.url.path,
            exc.status_code,
            exc.detail,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(f"HTTP_{exc.status_code}", str(exc.detail)),
        )

    @app.exception_handler(SQLAlchemyError)
    async def _handle_sqlalchemy_error(
        request: Request, exc: SQLAlchemyError
    ) -> JSONResponse:
        """数据库异常：记录完整堆栈，对外只返回通用提示。"""
        logger.exception("数据库异常 | path=%s", request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body("DATABASE_ERROR", "数据库操作失败，请稍后重试"),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """兜底处理：任何未捕获异常都不允许把堆栈返回给客户端。"""
        logger.exception("未处理异常 | path=%s", request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body("INTERNAL_ERROR", "服务器内部错误，请联系管理员"),
        )
