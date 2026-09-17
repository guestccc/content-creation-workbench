"""
vct — 视频内容制作工具箱

把本目录下的两个独立工具统一成一个中文命令行入口：

* VideoCaptioner          —— 语音转字幕、字幕优化/翻译、烧录字幕、TTS 配音
* video-subtitle-remover  —— 擦除视频中的硬字幕/水印

核心流程只使用 Python 标准库；只有交互菜单用到了 questionary 来提供方向键选择，
而且它装在 vct 自己的解释器里，不会碰两个工具的虚拟环境。questionary 缺失、
输出被重定向、或终端过小时，交互会自动回退到纯标准库的输入方式
（见 vctl/ui.py 的 rich_enabled）。

真正的重活交给这两个工具各自的解释器去跑（见 vctl/env.py）。
"""

__version__ = "1.1.0"

# 工具在终端里的展示名，多个模块共用
TOOL_NAME = "视频内容制作工具箱"
TOOL_CMD = "vct"
