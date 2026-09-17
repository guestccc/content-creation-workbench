"""日志配置模块。

统一日志格式，保证本地排查与线上采集使用同一套排版。
"""

import logging
import sys

from app.core.config import settings

# 统一格式：时间 | 级别 | 模块 | 内容
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 幂等标记，避免测试中多次调用导致 handler 重复叠加
_configured = False


def setup_logging() -> None:
    """初始化根日志器，重复调用安全。"""
    global _configured
    if _configured:
        return

    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn 自带日志器改为向上传递，避免出现两种排版风格
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """按模块名获取日志器。"""
    return logging.getLogger(name)
