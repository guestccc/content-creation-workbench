"""本地文件系统浏览接口。

存在的理由：浏览器拿不到本地绝对路径（<input type="file"> 只给文件名），
所以「选输入/输出目录」必须由后端列目录。

安全边界（按需求有意放开根目录限制后，仍保留的底线）：
- 只读：只返回名称/类型/大小，绝不返回任何文件内容；
- 路径必须真实存在且是目录，否则 400；
- 单次最多返回 SCENE_FS_LIST_LIMIT 条，超出截断并置 truncated 标记。

⚠️ 若日后把 HOST 改成 0.0.0.0 暴露到局域网，本接口必须先加鉴权
或恢复根目录白名单 —— 现在它依赖「仅监听 127.0.0.1 的单机工具」这个前提。
"""

from pathlib import Path

from fastapi import APIRouter, Query

from app.core.config import settings
from app.core.exceptions import BadRequestError
from app.core.logging import get_logger
from app.schemas.common import ApiResponse
from app.schemas.scene_job import FsEntry, FsListData
from app.services.scene_job_service import is_video_file

router = APIRouter(prefix="/fs", tags=["文件系统"])
logger = get_logger(__name__)


@router.get("/list", response_model=ApiResponse[FsListData], summary="列目录")
def list_directory(
    path: str = Query(default="~", description="要列出的目录，支持 ~ 开头，默认用户主目录"),
) -> ApiResponse[FsListData]:
    """列出一个目录的内容：目录在前、同类按名称排序，视频文件单独标记。"""
    cleaned = path.strip() or "~"
    target = Path(cleaned).expanduser()

    if not target.exists():
        raise BadRequestError(f"路径不存在：{cleaned}")
    if not target.is_dir():
        raise BadRequestError(f"路径不是目录：{cleaned}")

    entries: list[FsEntry] = []
    truncated = False
    video_count = 0
    try:
        # 按名称预排序再截断，保证多次请求看到的顺序稳定
        children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        for child in children:
            # 隐藏文件不展示，也避免把 ._ 开头的 AppleDouble 文件当素材
            if child.name.startswith("."):
                continue
            if len(entries) >= settings.SCENE_FS_LIST_LIMIT:
                truncated = True
                break
            is_dir = child.is_dir()
            is_video = (not is_dir) and is_video_file(child.name)
            if is_video:
                video_count += 1
            size: int | None = None
            if not is_dir:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = None
            entries.append(
                FsEntry(
                    name=child.name,
                    path=str(child),
                    is_dir=is_dir,
                    is_video=is_video,
                    size_bytes=size,
                )
            )
    except PermissionError as exc:
        raise BadRequestError(f"没有权限读取目录：{cleaned}") from exc

    parent = str(target.parent) if target.parent != target else None

    return ApiResponse(
        data=FsListData(
            path=str(target),
            parent=parent,
            entries=entries,
            truncated=truncated,
            video_count=video_count,
        )
    )
