"""媒体清单的枚举与校验（视频 / 图片共用一套）。

这段逻辑原本长在 `scene_job_service.py` 里。字幕提取要做一模一样的事
（同样是「输入可以是文件也可以是目录、可以只处理前端勾选的那几条、
要跳过隐藏文件和 macOS 的 AppleDouble 文件、要有批量上限」），再抄一份
就会出现两个副本 —— 和 `media_tools.py` 开头写的是同一个理由。
一键换背景（图片）是第三个用户，同样的理由第三次成立，于是从「视频专用」
泛化成「媒体通用」。

差别只有三处，都做成入参：
- `extensions`：算不算可处理，由各自功能的白名单决定（SCENE_ / SUBTITLE_ /
  BACKGROUND_ 前缀的配置）；
- `max_files`：一次最多处理多少条，各功能的默认值一样但配置项是分开的；
- `kind`：**只影响错误文案**。文案写死「视频」的话，图片功能报错时会蹦出
  「没有可处理的视频文件」—— 用户对着一个全是 PNG 的目录只会一头雾水。

`scene_job_service` / `subtitle_job_service` 保留同名的薄封装
（`api/v1/fs.py` 等调用方不必改 import），行为与本模块完全一致。
"""

from pathlib import Path
from typing import List, Optional, Sequence

from app.core.exceptions import BadRequestError

#: 视频后缀 → Content-Type。
#:
#: 播放接口必须给对类型：浏览器对 `application/octet-stream` 的处理是**下载**
#: 而不是播放，`<video>` 拿到这种类型只会黑屏。
#: 这里不认识的后缀一律退回 octet-stream —— 白名单之外的文件本来也不该走到
#: 播放接口（大小写不敏感，后缀统一小写后查表）。
VIDEO_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
}

#: 图片后缀 → Content-Type。
#:
#: 与视频同理：给错类型，`<img>` 拿到 octet-stream 不显示，浏览器直接下载。
#:
#: ⚠️ **白名单里刻意不放 `.svg`**。SVG 是唯一能携带脚本的图片格式，从素材目录
#: 直出到同源页面就是一条 XSS 面；普通位图没有这个性质。本工具的素材目录装的
#: 是用户自己的图，但「用户自己拷进来的」不等于「可信」—— 从网上存的 SVG
#: 一样是外来的。要支持的话得单独走一条净化路径，不在这份白名单里开。
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def media_type_for_video(name: str) -> str:
    """按扩展名取视频的 Content-Type。

    扩展名在不在白名单是调用方的事（`is_media_file`），这里只负责查表 ——
    白名单里加了新后缀却忘了加类型时，结果是「浏览器自己嗅探」而不是 KeyError。
    """
    return VIDEO_MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def media_type_for_image(name: str) -> str:
    """按扩展名取图片的 Content-Type。查不到退回 octet-stream（同视频的理由）。"""
    return IMAGE_MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


def is_media_file(name: str, extensions: Sequence[str]) -> bool:
    """按扩展名白名单判断是否为可处理的媒体文件。

    只按扩展名判断，不探文件头 —— 这条判断要跑在每一个目录条目上，
    而且真正下判断的是后面的处理程序本身，这里只负责把明显不相干的挡掉。
    """
    return Path(name).suffix.lower() in extensions


def enumerate_media_files(
    input_path: str,
    *,
    recursive: bool,
    extensions: Sequence[str],
    max_files: int,
    kind: str = "视频",
    files: Optional[List[str]] = None,
) -> List[Path]:
    """把输入路径展开成一份确定的媒体清单。

    规则：
    - 输入是文件：直接用这一条（扩展名不在白名单也接受 —— 用户显式点名了它）；
    - 输入是目录：按扩展名白名单枚举，recursive 决定是否下钻子目录；
      跳过隐藏文件和 macOS 的 AppleDouble（._ 开头）文件；
    - 传了 files（前端勾选的文件名）时只保留勾选项。

    Args:
        input_path: 输入的文件或目录（绝对路径）。
        recursive: 输入为目录时是否下钻子目录。
        extensions: 扩展名白名单（小写、带点）。
        max_files: 单次任务的批量上限。
        kind: 「视频」/「图片」这类名词，**只用于错误文案**。默认「视频」是为了
            让既有调用方一个字都不用改。
        files: 前端勾选的文件名列表；为空表示整个目录都要。

    Returns:
        排序后的路径列表。

    Raises:
        BadRequestError: 路径不存在、既不是文件也不是目录、目录里没有可处理的
            文件、勾选的文件名在目录中不存在、或条数超过上限。
    """
    source = Path(input_path)

    if not source.exists():
        raise BadRequestError(f"输入路径不存在：{input_path}")

    if source.is_file():
        return [source]

    if not source.is_dir():
        raise BadRequestError(f"输入路径既不是文件也不是目录：{input_path}")

    # 目录：枚举文件
    if files:
        # 前端勾选模式：只取勾选项，文件名已在 schema 层禁止路径分隔符
        candidates = [source / name for name in files]
        missing = [p.name for p in candidates if not p.is_file()]
        if missing:
            raise BadRequestError(f"以下文件在输入目录中不存在：{missing}")
        found = candidates
    else:
        iterator = source.rglob("*") if recursive else source.iterdir()
        found = [
            entry
            for entry in iterator
            if entry.is_file()
            and not entry.name.startswith(".")
            and is_media_file(entry.name, extensions)
        ]

    found.sort(key=lambda p: str(p).lower())

    if not found:
        raise BadRequestError(f"输入目录下没有可处理的{kind}文件：{input_path}")
    if len(found) > max_files:
        raise BadRequestError(
            f"一次最多处理 {max_files} 条{kind}，当前共 {len(found)} 条；"
            "请改用文件勾选或分批处理"
        )
    return found


def enumerate_videos(
    input_path: str,
    *,
    recursive: bool,
    files: Optional[List[str]] = None,
    extensions: Sequence[str],
    max_files: int,
) -> List[Path]:
    """视频清单（`enumerate_media_files` 的薄封装，`kind` 固定为「视频」）。"""
    return enumerate_media_files(
        input_path,
        recursive=recursive,
        files=files,
        extensions=extensions,
        max_files=max_files,
        kind="视频",
    )


def enumerate_images(
    input_path: str,
    *,
    recursive: bool,
    files: Optional[List[str]] = None,
    extensions: Sequence[str],
    max_files: int,
) -> List[Path]:
    """图片清单（`enumerate_media_files` 的薄封装，`kind` 固定为「图片」）。

    一键换背景用它。与视频唯一的差别就是错误文案里的名词 —— 对着一个全是
    PNG 的目录说「没有可处理的视频文件」是纯粹的误导。
    """
    return enumerate_media_files(
        input_path,
        recursive=recursive,
        files=files,
        extensions=extensions,
        max_files=max_files,
        kind="图片",
    )
