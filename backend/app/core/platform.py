"""当前系统的简写与显示名：平台判断的**单一来源**。

原本长在 `services/subtitle_env.py` 里，只有安装指引一个使用方。现在需要按平台
分支的地方变多了（用户级环境变量怎么设、Voicebox 桌面端装在哪、安装指引怎么写），
再复制几份 `os.name == "nt"` 就会出现多个副本 —— 复制出去的那几份只会继承代码、
不继承下面这条教训。

**为什么用 `os.name` 而不是 `sys.platform` 判 Windows**：`sys.platform` 在
Cygwin 上是 `cygwin`、在 MSYS2 上是 `msys`，两者都不是 `win32`，却跑在 Windows 上；
`os.name` 在这两种环境下同样是 `posix` —— 但真正的坑不在判别本身，而在于**测试
会整体替换掉 `os` 替身**（见 subtitle_env 里 `os.name` 那条注释）。所以判别逻辑
只写在这一处，别处一律调这里的函数，测试也只需要替换**使用方模块**里的这个名字。

约定（后续新增模块照做）：

- 需要判平台时 `from app.core.platform import platform_key`，并在**运行时**调裸名
  `platform_key()`；
- 测试按仓库既有范式整体替换：`monkeypatch.setattr(<使用方模块>, "platform_key",
  lambda: "windows")`。这就要求调用方在模块顶层 `from ... import platform_key`
  （模块属性可替换），而不是写 `from app.core import platform` 再 `platform.key()`
  （那样替换的是 `app.core.platform.platform_key`，会同时影响所有使用方，
  也违背「测试只隔离被测模块」的惯例）。
"""

import os
import sys


def platform_key() -> str:
    """当前系统的简写：macos / windows / linux。

    探测跑在后端，而这是个本机工具（后端与浏览器在同一台机器），所以后端
    的系统就是用户要照着装的那个系统 —— 指引只需要出当前系统这一份。
    """
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def platform_label() -> str:
    """给用户看的系统名。"""
    return {"macos": "macOS", "windows": "Windows", "linux": "Linux"}[platform_key()]
