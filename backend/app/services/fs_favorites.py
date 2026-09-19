"""目录收藏夹：目录选择弹窗里「常用目录」的持久化。

与 mix_library 的素材目录注册表同一套做法（JSON + tmp/os.replace 原子写 +
损坏容错 + id 用 sha1(绝对路径)[:16]），区别只有两点：

- **不 seed 默认收藏**：素材目录有 materials/clips 这个天然默认值，收藏夹没有；
  塞一个用户没挑过的目录只会让人莫名其妙。
- **文件不存在时不落盘**：只是打开弹窗看一眼，不该在磁盘上留下文件。

文件落在素材根（``.directory-favorites.json``），与 ``.mix-sources.json`` 同处：
materials/ 已被 .gitignore 整体挡住，收藏的绝对路径不会进仓库；
也**绝不往被收藏的用户目录里写任何东西**。

路径一律 expanduser + resolve 后再算 id 与入库：resolve 会消掉 ``..``、符号链接 /
junction，并把 Windows 上的盘符与目录名规范成磁盘真实大小写 ——
「同一个目录只收藏一条」因此不依赖字符串怎么写的。代价是收藏 junction
（如 D:\\clips → C:\\data\\clips）会落到物理目录 —— 收藏的是物理目录，
保留用户写法就等于放弃幂等。

并发前提：读-改-写持模块级锁，只保证单进程内安全（本工具 127.0.0.1 单 worker，
够用）。写盘失败不重试，直接 400 让用户再点一次（与 mix 的 _save_registry 一致）。

全局一份收藏清单（不区分弹窗用途）：用户的心智是「这些是我常去的目录」，
而不是「这是混剪输出用的收藏」。数据形状留了扩展余地，将来要分组再加字段。
"""

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.core.config import settings
from app.core.exceptions import BadRequestError
from app.core.logging import get_logger
from app.core.materials import materials_root

logger = get_logger(__name__)

#: 收藏夹文件名（放素材根下，被 .gitignore 挡住）。
FAVORITES_FILENAME = ".directory-favorites.json"

#: 读-改-写的进程内锁。FastAPI 同步路由跑在线程池里，真并发会丢更新。
_LOCK = threading.Lock()


def _registry_path() -> Path:
    """收藏夹文件路径。"""
    return materials_root() / FAVORITES_FILENAME


def favorite_id_for(path: Path | str) -> str:
    """收藏 id：与片段/素材目录 id 同一套算法（sha1(绝对路径)[:16]，URL-safe）。"""
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]


def _describe(entry: Dict[str, Any]) -> Dict[str, Any]:
    """把条目补成给前端的形状（exists 每次都重新判断，拔盘/删目录能提示）。

    name 是目录名；盘符根（E:\\）的 ``path.name`` 为空串，回退成完整路径，
    侧栏不能出现空白条目。
    """
    path = Path(entry["path"])
    return {
        "id": entry.get("id") or favorite_id_for(path),
        "path": str(path),
        "name": path.name or str(path),
        "exists": path.is_dir(),
        "added_at": entry.get("added_at") or 0.0,
    }


def _entry_for(path: Path) -> Dict[str, Any]:
    """新建一条收藏条目。"""
    return {"id": favorite_id_for(path), "path": str(path), "added_at": time.time()}


def _save(entries: List[Dict[str, Any]]) -> bool:
    """原子写收藏夹：先写临时文件再 os.replace。Returns: 是否写成功。"""
    path = _registry_path()
    tmp_path = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(
            json.dumps({"version": 1, "favorites": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
        return True
    except OSError as exc:
        logger.warning("目录收藏夹写盘失败：%s（%s）", path, exc)
        tmp_path.unlink(missing_ok=True)
        return False


def _load() -> List[Dict[str, Any]]:
    """读收藏夹；文件不存在返回空列表且**不落盘**（打开弹窗看一眼不该留文件）。"""
    path = _registry_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw.get("favorites") if isinstance(raw, dict) else None
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict) and item.get("path")]
        logger.warning("目录收藏夹结构不对，按空列表处理：%s", path)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("目录收藏夹损坏，按空列表处理：%s", path)
    return []


def list_favorites() -> List[Dict[str, Any]]:
    """收藏清单（按收藏顺序）。"""
    with _LOCK:
        return [_describe(entry) for entry in _load()]


def add_favorite(raw_path: str) -> Tuple[Dict[str, Any], bool]:
    """收藏一个目录。

    Args:
        raw_path: 用户给的目录路径（支持 ~ 开头；必须是绝对路径）。
    Returns:
        (条目, 是否新建)。目录已收藏过时原样返回（幂等），不会重复登记。
    Raises:
        BadRequestError: 路径为空/非绝对/不存在/不是目录/数量超限/写盘失败。
    """
    text = (raw_path or "").strip()
    if not text:
        raise BadRequestError("目录路径不能为空")

    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise BadRequestError(f"请填写绝对路径：{text}")
    try:
        # resolve 同时消掉 .. 与符号链接、统一盘符大小写，保证「同一目录只收藏一条」
        resolved = candidate.resolve()
    except OSError as exc:
        raise BadRequestError(f"目录无法解析：{text}（{exc}）") from exc
    if not resolved.exists():
        raise BadRequestError(f"目录不存在：{resolved}")
    if not resolved.is_dir():
        raise BadRequestError(f"这不是一个目录：{resolved}")

    with _LOCK:
        entries = _load()
        favorite_id = favorite_id_for(resolved)
        for entry in entries:
            if entry.get("id") == favorite_id or entry.get("path") == str(resolved):
                return _describe(entry), False

        if len(entries) >= settings.FS_MAX_FAVORITES:
            raise BadRequestError(
                f"收藏目录最多 {settings.FS_MAX_FAVORITES} 个，请先移除不用的目录"
            )

        entry = _entry_for(resolved)
        if not _save(entries + [entry]):
            raise BadRequestError("收藏保存失败，请确认素材目录可写")

        logger.info("收藏目录 | %s", resolved)
        return _describe(entry), True


def remove_favorite(favorite_id: str) -> bool:
    """取消收藏（**磁盘上的目录与文件一律不动**）。

    Returns:
        是否移除成功（id 不存在时返回 False）。
    Raises:
        BadRequestError: 写盘失败。
    """
    with _LOCK:
        entries = _load()
        kept = [entry for entry in entries if entry.get("id") != favorite_id]
        if len(kept) == len(entries):
            return False
        if not _save(kept):
            raise BadRequestError("收藏保存失败，请确认素材目录可写")
        logger.info("取消收藏目录 | id=%s", favorite_id)
        return True
