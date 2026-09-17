"""
支持 `python3 -m vctl` 的入口。

正常情况下用工具箱里的 `vct` 脚本启动，这个文件是给
「不想用脚本、直接敲 python -m vctl」的场景兜底的。
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
