"""CLI 的异常类型。

统一继承 CwError，这样 UI 层可以用一个 except 兜住所有「能给出人话解释」的错误，
把意外异常（真正的 bug）留给调用栈自己暴露。
"""


class CwError(Exception):
    """所有可预期错误的基类。"""


class ConfigError(CwError):
    """配置文件内容不合法（例如 PORT 不是整数）。"""


class ProcessError(CwError):
    """进程操作失败（启动、停止、或 pidfile 内容损坏）。"""


class PrecheckError(CwError):
    """启动前的环境检查未通过。"""
