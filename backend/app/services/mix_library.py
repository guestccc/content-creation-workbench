"""混剪素材库：扫描用户自己添加的素材目录。

与「智能镜头分割」的任务历史完全解耦 —— 直接扫磁盘，任务记录删了素材依然在。

**素材目录由用户挑**，不限定在 materials/ 里：`materials/clips/` 只是首次运行时
写进注册表的默认目录，用户可以在页面上删掉它、也可以加任意目录
（外接硬盘上的原片目录、其他剪辑工程的导出目录……都行）。

三个内部产物一律藏在素材根下，**不往用户自己的目录里写东西**：

- `.mix-sources.json`  素材目录注册表（用户添加过哪些目录）
- `.mix-index.json`    时长索引缓存
- `.mix-thumbs/`       缩略图缓存

片段 id 是 `sha1(绝对路径)[:16]` —— 用 id 而不是路径做接口参数，同时解决三件事：
URL 里不出现中文、长度可控、**杜绝路径穿越**（请求 id 时重新扫描比对，命中才返回文件；
扫不到就是 404）。这与 scene_jobs.py 里「接口永不接受前端传入路径」的安全原则同源。

时长索引缓存的理由：单次 ffprobe 实测约 44ms，171 条串行要 7.5 秒 —— 首次扫描用
4 线程并发（约 2 秒）后落盘缓存，之后秒开。按 `(mtime, size)` 逐条失效，写盘用
tmp + os.replace 原子替换，读到损坏 JSON 就整体重建（与 process.write_handle 同款做法）。
"""

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from app.core.config import settings
from app.core.logging import get_logger
from app.core.materials import CLIPS, materials_root, subdir
from app.services.media_tools import probe_duration

logger = get_logger(__name__)

#: 素材目录注册表文件名（放素材根下，随素材一起走、也被 .gitignore 挡住）。
REGISTRY_FILENAME = ".mix-sources.json"
#: 时长索引缓存文件名（中央一份，键是绝对路径）。
INDEX_FILENAME = ".mix-index.json"
#: 缩略图缓存目录名（中央一份，不污染用户目录）。
THUMB_DIR_NAME = ".mix-thumbs"
#: 首次全量探测时的并发线程数。ffprobe 是子进程调用，多线程在等待时释放 GIL。
PROBE_THREADS = 4


# --------------------------------------------------------------------------
# 路径与 id
# --------------------------------------------------------------------------


def _registry_path() -> Path:
    """素材目录注册表路径。"""
    return materials_root() / REGISTRY_FILENAME


def _index_path() -> Path:
    """时长索引缓存路径。"""
    return materials_root() / INDEX_FILENAME


def thumb_cache_dir(*, create: bool = True) -> Path:
    """缩略图缓存目录（中央一份，避免往用户自己的目录里写东西）。

    Args:
        create: 是否顺带建目录。扫描时只想看看、不想建，就传 False。
    """
    path = materials_root() / THUMB_DIR_NAME
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("缩略图缓存目录创建失败：%s（%s）", path, exc)
    return path


def thumb_path_for(clip_id: str) -> Path:
    """某个片段的缩略图缓存路径。"""
    return thumb_cache_dir() / f"{clip_id}.jpg"


def source_id_for(path: Path | str) -> str:
    """素材目录 id：与片段 id 同一套算法（sha1(绝对路径)[:16]，URL-safe）。"""
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]


def clip_id_for(abs_path: Path | str) -> str:
    """由**绝对路径**算出片段 id。

    用 sha1 取前 16 位：碰撞概率对本场景（几千条）可忽略，
    且天然 URL-safe、固定长度、不含中文与路径分隔符。
    """
    return hashlib.sha1(str(abs_path).encode("utf-8")).hexdigest()[:16]


def _is_video(path: Path) -> bool:
    """是否视频文件：后缀在白名单内。隐藏文件在遍历时就已经被跳过。"""
    return path.suffix.lower() in settings.SCENE_INPUT_EXTENSIONS


# --------------------------------------------------------------------------
# 素材目录注册表
# --------------------------------------------------------------------------


def _describe(entry: Dict[str, Any]) -> Dict[str, Any]:
    """把注册表条目补成给前端的形状（exists 每次都重新判断，拔盘/删目录能提示）。"""
    path = Path(entry["path"])
    return {
        "id": entry.get("id") or source_id_for(path),
        "path": str(path),
        "name": path.name or str(path),
        "exists": path.is_dir(),
        "added_at": entry.get("added_at") or 0.0,
    }


def _entry_for(path: Path) -> Dict[str, Any]:
    """新建一条注册表条目。"""
    return {"id": source_id_for(path), "path": str(path), "added_at": time.time()}


def _default_sources() -> List[Dict[str, Any]]:
    """首次运行的默认素材目录：镜头切片目录（不存在就不给，页面引导用户自己添加）。"""
    clips = subdir(CLIPS)
    return [_entry_for(clips)] if clips.is_dir() else []


def _save_registry(entries: List[Dict[str, Any]]) -> bool:
    """原子写注册表：先写临时文件再 os.replace。Returns: 是否写成功。"""
    path = _registry_path()
    tmp_path = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(
            json.dumps({"version": 1, "sources": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
        return True
    except OSError as exc:
        logger.warning("素材目录注册表写盘失败：%s（%s）", path, exc)
        tmp_path.unlink(missing_ok=True)
        return False


def _load_registry() -> List[Dict[str, Any]]:
    """读素材目录注册表；首次运行时用默认目录初始化并落盘。

    「文件不存在」与「存在但是空列表」是两种含义：前者是首次运行（给一份默认目录），
    后者是用户把目录都删光了（保持为空 —— 不能自作主张加回来，否则用户删不掉）。
    """
    path = _registry_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw.get("sources") if isinstance(raw, dict) else None
        if isinstance(items, list):
            return [it for it in items if isinstance(it, dict) and it.get("path")]
        logger.warning("素材目录注册表结构不对，按默认目录重建：%s", path)
    except FileNotFoundError:
        seeded = _default_sources()
        _save_registry(seeded)
        return seeded
    except (OSError, ValueError):
        logger.warning("素材目录注册表损坏，按默认目录重建：%s", path)
    seeded = _default_sources()
    _save_registry(seeded)
    return seeded


def list_sources() -> List[Dict[str, Any]]:
    """已添加的素材目录清单（按添加顺序）。"""
    return [_describe(entry) for entry in _load_registry()]


def add_source(raw_path: str) -> Tuple[Dict[str, Any], bool]:
    """添加一个素材目录。

    Args:
        raw_path: 用户给的目录路径（支持 ~ 开头；必须是绝对路径）。
    Returns:
        (目录信息, 是否新建)。目录已经加过时原样返回（幂等），不会重复注册。
    Raises:
        ValueError: 路径为空/非绝对/不存在/不是目录/数量超限/写盘失败。
    """
    text = (raw_path or "").strip()
    if not text:
        raise ValueError("目录路径不能为空")

    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"请填写绝对路径：{text}")
    try:
        # resolve 同时消掉 .. 与符号链接，保证「同一个目录只注册一次」
        resolved = candidate.resolve()
    except OSError as exc:
        raise ValueError(f"目录无法解析：{text}（{exc}）") from exc
    if not resolved.exists():
        raise ValueError(f"目录不存在：{resolved}")
    if not resolved.is_dir():
        raise ValueError(f"这不是一个目录：{resolved}")

    entries = _load_registry()
    source_id = source_id_for(resolved)
    for entry in entries:
        if entry.get("id") == source_id or entry.get("path") == str(resolved):
            return _describe(entry), False

    if len(entries) >= settings.MIX_MAX_SOURCES:
        raise ValueError(
            f"素材目录最多添加 {settings.MIX_MAX_SOURCES} 个，请先移除不用的目录"
        )

    entry = _entry_for(resolved)
    if not _save_registry(entries + [entry]):
        raise ValueError("素材目录保存失败，请确认素材目录可写")

    logger.info("添加混剪素材目录 | %s", resolved)
    return _describe(entry), True


def remove_source(source_id: str) -> bool:
    """把素材目录从注册表里移除（**磁盘上的文件一律不动**）。

    Returns: 是否移除成功（id 不存在时返回 False）。
    Raises:
        ValueError: 注册表写盘失败。
    """
    entries = _load_registry()
    kept = [entry for entry in entries if entry.get("id") != source_id]
    if len(kept) == len(entries):
        return False
    if not _save_registry(kept):
        raise ValueError("素材目录保存失败，请确认素材目录可写")
    logger.info("移除混剪素材目录 | id=%s", source_id)
    return True


# --------------------------------------------------------------------------
# 目录遍历
# --------------------------------------------------------------------------


def _iter_videos(root: Path, max_depth: int) -> Iterator[Path]:
    """递归枚举目录下的视频文件：跳过点开头的文件/目录，且限制深度。

    深度上限是必需的：用户完全可能把主目录整个加进来，
    不设上限就会把整块盘翻一遍。
    """
    stack: List[Tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            # 权限不足之类的目录直接跳过，不能因为一个子目录整批扫不出来
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    if depth + 1 <= max_depth:
                        stack.append((entry, depth + 1))
                elif entry.is_file() and _is_video(entry):
                    yield entry
            except OSError:
                continue


def _scan_source(root: Path) -> Tuple[List[Path], bool]:
    """扫描单个素材目录。Returns: (文件清单, 是否因超限被截断)。"""
    limit = settings.MIX_MAX_CLIPS_PER_SOURCE
    files: List[Path] = []
    truncated = False
    for file in _iter_videos(root, settings.MIX_SOURCE_SCAN_DEPTH):
        if len(files) >= limit:
            truncated = True
            break
        files.append(file)
    files.sort(key=str)
    return files, truncated


def _depth_of(raw_path: str) -> Tuple[int, int]:
    """目录的「具体程度」排序键：层数多的更具体，层数相同再比字符串长度。"""
    parts = len(Path(raw_path).parts)
    return (parts, len(raw_path))


def _rel_posix(file: Path, root: Path) -> str:
    """文件相对素材目录的 POSIX 路径（拿不到就退回文件名）。"""
    try:
        return file.relative_to(root).as_posix()
    except ValueError:
        return file.name


def _iter_all_files() -> Iterator[Path]:
    """遍历所有已注册素材目录下的视频文件（解析 id 用）。"""
    for source in _load_registry():
        root = Path(source.get("path", ""))
        if not root.is_dir():
            continue
        files, _truncated = _scan_source(root)
        yield from files


# --------------------------------------------------------------------------
# 时长索引缓存
# --------------------------------------------------------------------------


def _load_index() -> Dict[str, Dict[str, Any]]:
    """读时长索引缓存；文件不存在或损坏都按「没有缓存」处理（整体重建）。"""
    path = _index_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("混剪时长索引缓存损坏，将整体重建：%s", path)
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return entries if isinstance(entries, dict) else {}


def _save_index(entries: Dict[str, Dict[str, Any]]) -> None:
    """原子写时长索引缓存：写失败只变慢，不阻断主流程。"""
    path = _index_path()
    tmp_path = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(
            json.dumps({"version": 2, "entries": entries}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.warning("混剪时长索引缓存写盘失败：%s（%s）", path, exc)
        tmp_path.unlink(missing_ok=True)


def _fill_durations(files: List[Path]) -> Dict[str, Optional[float]]:
    """给每个文件补时长：先查缓存（按 mtime+size 失效），miss 的并发 ffprobe。

    探测失败（损坏的视频）返回 None，不阻断整个扫描。
    """
    cached = _load_index()
    # fresh 是「这次扫描之后索引应该长什么样」：命中的条目 + 新探测的条目。
    # 它天然完成了剪枝 —— 本次没扫到的素材不会进来，索引不会越攒越大。
    fresh: Dict[str, Dict[str, Any]] = {}
    durations: Dict[str, Optional[float]] = {}
    pending: List[Path] = []

    for file in files:
        key = str(file)
        try:
            stat = file.stat()
        except OSError:
            durations[key] = None
            continue
        hit = cached.get(key)
        if (
            isinstance(hit, dict)
            and hit.get("mtime") == stat.st_mtime
            and hit.get("size") == stat.st_size
            and isinstance(hit.get("duration"), (int, float, type(None)))
        ):
            durations[key] = hit["duration"]
            fresh[key] = hit
        else:
            pending.append(file)

    if pending:
        logger.info("混剪素材库：%s 条素材需要探测时长（并发 %s）", len(pending), PROBE_THREADS)

        def _probe(path: Path) -> Tuple[Path, Optional[float]]:
            return path, probe_duration(path)

        with ThreadPoolExecutor(max_workers=PROBE_THREADS) as pool:
            for file, duration in pool.map(_probe, pending):
                key = str(file)
                durations[key] = duration
                try:
                    stat = file.stat()
                except OSError:
                    continue
                fresh[key] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "duration": duration,
                }

    # 只有内容真的变了才写盘（页面上「重新扫描」是高频操作）
    if fresh != cached:
        _save_index(fresh)
    return durations


def _prune_thumbs(keep: Set[str]) -> None:
    """删掉不再被任何素材引用的缩略图缓存（按需重建，删了不心疼）。"""
    directory = thumb_cache_dir(create=False)
    if not directory.is_dir():
        return
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_file() and entry.stem not in keep:
            try:
                entry.unlink(missing_ok=True)
            except OSError:
                continue


# --------------------------------------------------------------------------
# 对外：扫描与解析
# --------------------------------------------------------------------------


def scan_library() -> Dict[str, Any]:
    """扫描所有已添加的素材目录，返回目录清单与片段清单（时长来自缓存，秒开）。

    Returns:
        {
            "sources": [{"id", "path", "name", "exists", "added_at",
                         "clip_count", "truncated"}],
            "clips": [{"id", "name", "group", "rel_path", "abs_path", "source_id",
                       "duration", "size_bytes"}],
            "scanned_at": <unix 时间戳>,
        }
    """
    sources = list_sources()
    scanned: Dict[str, Tuple[List[Path], bool]] = {}
    for source in sources:
        root = Path(source["path"])
        if not root.is_dir():
            # 目录被删了或盘没挂上：保留条目（文件回来就恢复），只是没有素材
            scanned[source["id"]] = ([], False)
            continue
        scanned[source["id"]] = _scan_source(root)

    # 跨目录去重：同一个文件可能被两个目录同时收录（比如把 materials 和 materials/clips
    # 都加进来）。归**更具体的那个目录**，不归先加进来的那个 ——
    # 反过来的话，「把父目录里的一组单独拎出来加一遍」这个最自然的用法会得到一条素材都没有，
    # 因为父目录早就把它的文件登记走了。
    seen: Set[str] = set()
    claimed: Dict[str, List[Path]] = {}
    for source in sorted(sources, key=lambda s: _depth_of(s["path"]), reverse=True):
        picked: List[Path] = []
        for file in scanned[source["id"]][0]:
            key = str(file)
            if key in seen:
                continue
            seen.add(key)
            picked.append(file)
        claimed[source["id"]] = picked

    # 输出仍按注册顺序（页面上目录下拉的顺序就是用户添加的顺序）
    per_source: List[Tuple[Dict[str, Any], List[Path], bool]] = [
        (source, claimed[source["id"]], scanned[source["id"]][1]) for source in sources
    ]

    all_files = [file for _source, files, _t in per_source for file in files]
    durations = _fill_durations(all_files)

    clips: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    for source, files, truncated in per_source:
        root = Path(source["path"])
        for file in files:
            rel_path = _rel_posix(file, root)
            parent = str(Path(rel_path).parent)
            try:
                size_bytes = file.stat().st_size
            except OSError:
                size_bytes = 0
            clips.append(
                {
                    "id": clip_id_for(file),
                    "name": file.name,
                    "group": "" if parent == "." else parent,
                    "rel_path": rel_path,
                    "abs_path": str(file),
                    "source_id": source["id"],
                    "duration": durations.get(str(file)),
                    "size_bytes": size_bytes,
                }
            )
        sources.append({**source, "clip_count": len(files), "truncated": truncated})

    _prune_thumbs({clip["id"] for clip in clips})
    return {"sources": sources, "clips": clips, "scanned_at": time.time()}


def resolve_clip(clip_id: str) -> Optional[Path]:
    """把片段 id 解析回磁盘上的绝对路径；解析不到返回 None（404）。

    安全模型：服务端重新扫描已注册的素材目录，只认扫描结果里出现过的 id ——
    就算 id 算法泄露，攻击者也无法用它读素材目录之外的任何文件。
    """
    if not clip_id:
        return None
    for file in _iter_all_files():
        if clip_id_for(file) == clip_id:
            return file
    return None


def resolve_clips(clip_ids: List[str]) -> Dict[str, Path]:
    """批量解析一组片段 id，返回 {clip_id: 绝对路径}（缺失的不在结果里）。

    只扫描一次目录 —— 创建任务时逐个调 resolve_clip 会扫 N 遍目录。
    """
    wanted = set(clip_ids)
    found: Dict[str, Path] = {}
    if not wanted:
        return found
    for file in _iter_all_files():
        cid = clip_id_for(file)
        if cid in wanted:
            found[cid] = file
    return found


def invalidate_cache() -> None:
    """删掉时长索引缓存，下次扫描整体重建（用于测试与排障）。"""
    _index_path().unlink(missing_ok=True)
