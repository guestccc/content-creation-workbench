"""视频清单的枚举与校验。

这段逻辑原本长在 `scene_job_service.py` 里。字幕提取要做一模一样的事
（同样是「输入可以是文件也可以是目录、可以只处理前端勾选的那几条、
要跳过隐藏文件和 macOS 的 AppleDouble 文件、要有批量上限」），再抄一份
就会出现两个副本 —— 和 `media_tools.py` 开头写的是同一个理由。

差别只有两处，都做成入参：
- `extensions`：算不算视频，由各自功能的白名单决定（SCENE_ / SUBTITLE_ 前缀的配置）；
- `max_files`：一次最多处理多少条，两个功能的默认值一样但配置项是分开的。

`scene_job_service` 保留同名的薄封装（`api/v1/fs.py` 等调用方不必改 import），
行为与本模块完全一致。
"""

from pathlib import Path
from typing import List, Optional, Sequence

from app.core.exceptions import BadRequestError


def is_video_file(name: str, extensions: Sequence[str]) -> bool:
    """按扩展名白名单判断是否为可处理的视频文件。

    只按扩展名判断，不探文件头 —— 这条判断要跑在每一个目录条目上，
    而且真正下判断的是后面的 CLI 本身，这里只负责把明显不相干的挡掉。
    """
    return Path(name).suffix.lower() in extensions


def enumerate_videos(
    input_path: str,
    *,
    recursive: bool,
    files: Optional[List[str]] = None,
    extensions: Sequence[str],
    max_files: int,
) -> List[Path]:
    """把输入路径展开成一份确定的视频清单。

    规则：
    - 输入是文件：直接用这一条（扩展名不在白名单也接受 —— 用户显式点名了它）；
    - 输入是目录：按扩展名白名单枚举，recursive 决定是否下钻子目录；
      跳过隐藏文件和 macOS 的 AppleDouble（._ 开头）文件；
    - 传了 files（前端勾选的文件名）时只保留勾选项。

    Args:
        input_path: 输入的文件或目录（绝对路径）。
        recursive: 输入为目录时是否下钻子目录。
        files: 前端勾选的文件名列表；为空表示整个目录都要。
        extensions: 视频扩展名白名单（小写、带点）。
        max_files: 单次任务的批量上限。

    Returns:
        排序后的视频路径列表。

    Raises:
        BadRequestError: 路径不存在、既不是文件也不是目录、目录里没有可处理的视频、
            勾选的文件名在目录中不存在、或条数超过上限。
    """
    source = Path(input_path)

    if not source.exists():
        raise BadRequestError(f"输入路径不存在：{input_path}")

    if source.is_file():
        return [source]

    if not source.is_dir():
        raise BadRequestError(f"输入路径既不是文件也不是目录：{input_path}")

    # 目录：枚举视频文件
    if files:
        # 前端勾选模式：只取勾选项，文件名已在 schema 层禁止路径分隔符
        candidates = [source / name for name in files]
        missing = [p.name for p in candidates if not p.is_file()]
        if missing:
            raise BadRequestError(f"以下文件在输入目录中不存在：{missing}")
        videos = candidates
    else:
        iterator = source.rglob("*") if recursive else source.iterdir()
        videos = [
            entry
            for entry in iterator
            if entry.is_file()
            and not entry.name.startswith(".")
            and is_video_file(entry.name, extensions)
        ]

    videos.sort(key=lambda p: str(p).lower())

    if not videos:
        raise BadRequestError(f"输入目录下没有可处理的视频文件：{input_path}")
    if len(videos) > max_files:
        raise BadRequestError(
            f"一次最多处理 {max_files} 条视频，当前共 {len(videos)} 条；"
            "请改用文件勾选或分批处理"
        )
    return videos
