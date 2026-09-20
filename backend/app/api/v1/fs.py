"""本地文件系统浏览与目录收藏接口。

存在的理由：浏览器拿不到本地绝对路径（<input type="file"> 只给文件名），
所以「选输入/输出目录」必须由后端列目录。

接口一览：

- ``GET    /fs/list``                        列目录
- ``GET    /fs/preview?path=``               本地视频只读流式预览（支持 Range）
- ``GET    /fs/favorites``                   收藏的目录
- ``POST   /fs/favorites``                   收藏目录（幂等）
- ``DELETE /fs/favorites/{favorite_id}``     取消收藏

静态路径要写在动态路径之前（``/favorites`` 在 ``/favorites/{favorite_id}`` 前面）。

安全边界（按需求有意放开根目录限制后，仍保留的底线）：
- 路径必须真实存在（列表还要求是目录），否则 400；
- 单次最多返回 SCENE_FS_LIST_LIMIT 条，超出截断并置 truncated 标记；
- 收藏只登记路径，不读取、不移动、不复制任何文件；取消收藏不动磁盘；
- ``/preview`` 是全模块唯一会吐文件内容的接口，因此**只吐视频**：绝对路径 +
  视频后缀白名单 + is_file，只读、不改写、不删除，并且只服务白名单内的后缀。

⚠️ 若日后把 HOST 改成 0.0.0.0 暴露到局域网，本模块必须先加鉴权
或恢复根目录白名单 —— 现在它依赖「仅监听 127.0.0.1 的单机工具」这个前提。
"""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import Response

from app.core.config import settings
from app.core.exceptions import BadRequestError, NotFoundError
from app.core.logging import get_logger
from app.core.materials import ensure_materials_layout
from app.core.video_files import media_type_for_video
from app.schemas.common import ApiResponse, absolutize_path
from app.schemas.scene_job import FsEntry, FsFavoriteCreate, FsFavoriteItem, FsListData
from app.services import fs_favorites
from app.services.file_range import ranged_file_response
from app.services.scene_job_service import is_video_file

router = APIRouter(prefix="/fs", tags=["文件系统"])
logger = get_logger(__name__)


def _resolve_target(path: Optional[str]) -> Path:
    """解析要列出的目录。

    显式传入的路径原样用（支持 ~ 开头）；不传则落到**素材目录**。

    不传 path 时列的是素材目录**根**（列出来就是 source / clips / subtitle /
    output / crawl / finalcut / dubbing 几个分段，一眼能看出素材该怎么放）。
    整个 materials/ 不在 git 里，
    新克隆的仓库上压根不存在，所以这里顺手把骨架建出来 —— 对着一个
    「路径不存在」的 400 只会让第一次用的人摸不着头脑。
    显式传进来的路径不做任何创建，保持「路径必须真实存在」的严格语义。
    """
    cleaned = (path or "").strip()
    if cleaned:
        return Path(cleaned).expanduser()
    return ensure_materials_layout()


@router.get("/list", response_model=ApiResponse[FsListData], summary="列目录")
def list_directory(
    path: Optional[str] = Query(
        default=None,
        description="要列出的目录，支持 ~ 开头；不传则列出素材目录（默认仓库根目录的 materials/）",
    ),
) -> ApiResponse[FsListData]:
    """列出一个目录的内容：目录在前、同类按名称排序，视频文件单独标记。"""
    target = _resolve_target(path)

    if not target.exists():
        raise BadRequestError(f"路径不存在：{target}")
    if not target.is_dir():
        raise BadRequestError(f"路径不是目录：{target}")

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
        raise BadRequestError(f"没有权限读取目录：{target}") from exc

    parent = str(target.parent) if target.parent != target else None

    try:
        # 收藏夹按 resolve 后的路径判重，前端判断「当前目录已收藏」要用它；
        # path 本身保留用户写法不动（junction/符号链接折叠会改变已保存路径的含义）
        canonical = str(target.resolve())
    except OSError:
        # 解析失败（网络盘掉线等）不阻断列目录：退回原样路径
        canonical = str(target)

    return ApiResponse(
        data=FsListData(
            path=str(target),
            canonical_path=canonical,
            parent=parent,
            entries=entries,
            truncated=truncated,
            video_count=video_count,
        )
    )


@router.get("/preview", summary="本地视频只读流式预览（支持 Range）")
def preview_local_video(
    request: Request,
    path: str = Query(..., description="视频文件绝对路径"),
) -> Response:
    """把本地视频以只读流发给 ``<video>``：整文件（200）或字节段（206 Partial Content）。

    素材列表里的「预览」按钮走它 —— 勾选之前先看一眼这条素材是不是想要的那条，
    免得选错了再等一遍切分。字幕提取页共用同一张卡片，因此也一并有了预览。

    后缀白名单与 ``/fs/list`` 判 ``is_video`` 用的是同一份配置：
    **列表里标成视频的，这里都播得出来**，不会出现「列得出来、点开 400」。

    安全边界与 ``/fs/list`` 同级：绝对路径 + 后缀白名单 + ``is_file()``，
    只读（不接收任何写操作）。前提是本工具仅监听 127.0.0.1。

    Raises:
        BadRequestError(400): 不是绝对路径、后缀不在白名单。
        NotFoundError(404): 文件不存在。
    """
    try:
        video = Path(absolutize_path(path, "视频文件"))
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc

    # is_video_file 是 scene_job_service 的薄封装（内部填的就是场景白名单），
    # 与 /fs/list 判 is_video 用的是同一个函数，规则不会漂
    if not is_video_file(video.name):
        raise BadRequestError(
            f"只支持 {' / '.join(settings.SCENE_INPUT_EXTENSIONS)} 文件，"
            f"当前是：{video.suffix or '（无扩展名）'}"
        )
    if not video.is_file():
        raise NotFoundError(f"视频文件不存在：{video.name}")

    # Range 由父进程按字节转发，媒体类型按后缀给 —— 给成 octet-stream
    # 浏览器会当成附件下载，<video> 只会黑屏
    return ranged_file_response(
        video,
        request.headers.get("range"),
        media_type=media_type_for_video(video.name),
    )


# 静态路径写在动态路径之前（/favorites 先于 /favorites/{favorite_id}）


@router.get("/favorites", response_model=ApiResponse[list[FsFavoriteItem]], summary="收藏的目录")
def list_favorites() -> ApiResponse[list[FsFavoriteItem]]:
    """目录选择弹窗左侧的收藏列表（按收藏顺序）。"""
    return ApiResponse(data=fs_favorites.list_favorites())


@router.post(
    "/favorites",
    response_model=ApiResponse[FsFavoriteItem],
    status_code=201,
    summary="收藏目录",
)
def create_favorite(payload: FsFavoriteCreate) -> ApiResponse[FsFavoriteItem]:
    """把目录加进收藏夹；重复收藏同一个目录是幂等的（返回已有条目）。

    Raises:
        BadRequestError(400): 路径为空/非绝对/不存在/不是目录/数量超限/写盘失败。
    """
    item, _created = fs_favorites.add_favorite(payload.path)
    return ApiResponse(data=item)


@router.delete("/favorites/{favorite_id}", response_model=ApiResponse[dict], summary="取消收藏")
def delete_favorite(
    favorite_id: str = PathParam(..., description="收藏 id"),
) -> ApiResponse[dict]:
    """取消收藏 —— **磁盘上的目录一个都不动**。

    Raises:
        NotFoundError(404): id 不存在；BadRequestError(400): 写盘失败。
    """
    if not fs_favorites.remove_favorite(favorite_id):
        raise NotFoundError(f"收藏不存在：{favorite_id}")
    return ApiResponse(data={"id": favorite_id})
