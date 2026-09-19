"""配音产物的磁盘侧：目录、索引、命名、删除。

产物落在 `materials/dubbing/`，一份音频一个文件。**磁盘是唯一的真相**：
页面上那份清单就是扫这个目录得到的，所以重启后端、或者用户自己往目录里
拷音频，清单都会如实反映 —— 不需要数据库表。

索引 `materials/dubbing/.dubbing-index.json` 只做**锦上添花**的事：记下这条
音频是用哪个音色、哪段文案生成的，以及它的时长（上游 /generate 会返回时长，
不必再 ffprobe）。索引里没有的文件（用户自己丢进来的）照样列出来，只是
`indexed=false`、没有音色/文案/时长。

写盘约定与 mix_library 的注册表一致：`threading.Lock` + 临时文件 + `os.replace`
原子替换，损坏的 JSON 降级成空索引并在下次写入时覆盖掉，绝不因此报错。
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from app.core.config import settings
from app.core.exceptions import BadRequestError
from app.core.logging import get_logger
from app.core.materials import DUBBING, subdir
from app.services.voicebox_client import AUDIO_EXTENSIONS

logger = get_logger(__name__)

#: 索引文件名（藏在产物目录里，随产物一起走；`materials/**` 已被 .gitignore 挡住）。
INDEX_FILENAME = ".dubbing-index.json"

#: 扩展名 → Content-Type（发给浏览器的 <audio> 用）。
MEDIA_TYPES: Dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
}

#: 文件名里不允许出现的字符：路径分隔符 + Windows 保留字符 + 控制字符占位。
#: 用户会给配音起中文名（「第一个卖点」），所以只挡真正会出问题的字符。
_ILLEGAL_CHARS = '<>:"/\\|?*'

#: 文件名（不含扩展名）的最大长度：给扩展名和 `-2` 后缀留出余量。
_MAX_BASE_LENGTH = 100

_index_lock = threading.Lock()


def dubbing_dir(*, create: bool = True) -> Path:
    """配音产物目录。列清单时传 create=False，避免「只是看看」也建目录。"""
    path = subdir(DUBBING)
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("配音产物目录创建失败：%s（%s）", path, exc)
    return path


def _index_path() -> Path:
    return dubbing_dir() / INDEX_FILENAME


# --------------------------------------------------------------------------
# 索引读写
# --------------------------------------------------------------------------


def _load_index() -> Dict[str, Dict[str, Any]]:
    """读索引。文件不存在或损坏都返回空索引 —— 索引只是附加信息。"""
    path = _index_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("配音索引读取失败，按空索引继续：%s（%s）", path, exc)
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {name: item for name, item in entries.items() if isinstance(item, dict)}


def _save_index(entries: Dict[str, Dict[str, Any]]) -> bool:
    """原子写索引：先写临时文件再 os.replace。Returns: 是否写成功。"""
    path = _index_path()
    tmp_path = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(
            json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
        return True
    except OSError as exc:
        logger.warning("配音索引写盘失败：%s（%s）", path, exc)
        tmp_path.unlink(missing_ok=True)
        return False


def write_entry(
    filename: str,
    *,
    duration: Optional[float],
    profile_id: str,
    profile_name: str,
    text_excerpt: str,
    size_bytes: int,
) -> None:
    """记一条产物到索引（生成成功后调用）。写失败不影响产物本身，只记日志。"""
    with _index_lock:
        entries = _load_index()
        entries[filename] = {
            "duration": duration,
            "profile_id": profile_id,
            "profile_name": profile_name,
            "text_excerpt": text_excerpt,
            "size_bytes": size_bytes,
            "created_at": time.time(),
        }
        _save_index(entries)


def drop_entry(filename: str) -> None:
    """从索引里摘掉一条（删产物时调用）。"""
    with _index_lock:
        entries = _load_index()
        if entries.pop(filename, None) is None:
            return
        _save_index(entries)


# --------------------------------------------------------------------------
# 命名
# --------------------------------------------------------------------------


def sanitize_base_name(raw: str) -> str:
    """把用户给的产物名清成一个安全的文件名（不含扩展名）。

    只挡真正会出问题的东西（路径分隔符、Windows 保留字符、首尾的点与空格），
    中文、空格、括号照常保留 —— 用户写「第一个卖点(定稿)」是合理的。
    """
    text = (raw or "").strip()
    for char in _ILLEGAL_CHARS:
        text = text.replace(char, "_")
    text = "".join(ch for ch in text if ch.isprintable())
    # Windows 上以点或空格结尾的文件名会被静默截断，先去干净
    text = text.strip(" .")
    return text[:_MAX_BASE_LENGTH]


def default_base_name() -> str:
    """没给文件名时的默认名：`dub_20260919-143012`。"""
    return f"dub_{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def allocate_path(base_name: str, extension: str) -> Path:
    """给产物分配一个不会覆盖别人的完整路径，重名的加 -2 / -3 后缀。

    沿用字幕产物的编号规则（services/subtitle_job_service.allocate_output_paths）：
    配音是「一次生成一份」的产物，用户很可能对同一段文案反复调音色，所以遇到
    重名换个名字而不是报错。预建目录的理由也一样 —— 写文件前目录得先在。
    """
    directory = dubbing_dir()
    ext = extension if extension in AUDIO_EXTENSIONS else ".wav"
    base = sanitize_base_name(base_name) or default_base_name()

    count = 0
    while True:
        count += 1
        candidate = directory / (f"{base}{ext}" if count == 1 else f"{base}-{count}{ext}")
        if not candidate.exists():
            return candidate


# --------------------------------------------------------------------------
# 清单与访问
# --------------------------------------------------------------------------


def audio_url_for(filename: str) -> str:
    """产物的音频流地址（后端给全，前端不拼 —— 与缩略图/视频流同一约定）。"""
    return f"{settings.API_V1_PREFIX}/voicebox/audios/{quote(filename)}/file"


def media_type_for(filename: str) -> str:
    """按扩展名给 Content-Type，认不出按 wav（上游默认产物就是 wav）。"""
    return MEDIA_TYPES.get(Path(filename).suffix.lower(), "audio/wav")


def resolve_audio(name: str) -> Optional[Path]:
    """把产物名解析成目录下的真实路径；不在目录里返回 None。

    安全说明：只接受**裸文件名**（不含分隔符、不是 . / ..）—— 路径完全由服务端
    在固定目录下拼，客户端无法借此读到别处去。这条与混剪「只收 clip_id」同源。

    Raises:
        BadRequestError: 名字里带了目录成分（拿路径来试探的，直接 400）。
    """
    text = (name or "").strip()
    if not text or text != Path(text).name or text in (".", ".."):
        raise BadRequestError(f"产物名不合法（只能是文件名）：{name}")
    if Path(text).suffix.lower() not in AUDIO_EXTENSIONS:
        raise BadRequestError(f"不是可识别的音频文件（{'/'.join(AUDIO_EXTENSIONS)}）：{text}")
    path = dubbing_dir(create=False) / text
    return path if path.is_file() else None


def _to_naive_utc(timestamp: float) -> datetime:
    """时间戳（秒）→ naive UTC datetime。

    项目里的时间字段一律是 naive UTC（SQLite 不存时区，见 models/content.utcnow），
    接口序列化时再由 schemas 的 to_utc_iso 补上 Z 后缀。
    """
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(tzinfo=None)


def list_audios() -> List[Dict[str, Any]]:
    """扫产物目录，套上索引信息，新的在前。"""
    directory = dubbing_dir(create=False)
    if not directory.is_dir():
        return []

    entries = _load_index()
    items: List[Dict[str, Any]] = []
    for path in directory.iterdir():
        if path.name.startswith(".") or not path.is_file():
            continue
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        try:
            stat = path.stat()
        except OSError:
            # 扫描瞬间被删掉/读不了的文件：跳过，别让整份清单挂掉
            continue
        size_bytes = stat.st_size
        entry = entries.get(path.name) or {}
        created_at = float(entry.get("created_at") or stat.st_mtime)
        items.append(
            {
                "name": path.name,
                "audio_url": audio_url_for(path.name),
                "size_bytes": size_bytes,
                "duration": entry.get("duration"),
                "profile_name": str(entry.get("profile_name") or ""),
                "text_excerpt": str(entry.get("text_excerpt") or ""),
                # 磁盘与索引里存的是时间戳（秒），出门换成 naive UTC datetime ——
                # 接口的时间一律走 to_utc_iso 那一套，前端 formatDateTime 直接吃
                "created_at": _to_naive_utc(created_at),
                "indexed": bool(entry),
            }
        )

    items.sort(key=lambda item: item["created_at"], reverse=True)
    return items[: settings.VOICEBOX_LIST_LIMIT]


def delete_audio(name: str) -> bool:
    """删掉一份产物（文件 + 索引条目）。Returns: 文件是否存在并删掉。

    Raises:
        BadRequestError: 名字不合法（见 resolve_audio）。
    """
    path = resolve_audio(name)
    if path is None:
        return False
    try:
        path.unlink()
    except OSError as exc:
        raise BadRequestError(f"删除产物失败：{path.name}（{exc}）") from exc
    drop_entry(path.name)
    return True
