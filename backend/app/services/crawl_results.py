"""素材抓取产物的读取与跨平台归一化。

MC 把一个任务的结果写进 `{save_data_path}/{platform}/jsonl/*_contents_*.jsonl`
（一行一条，字段是各平台原生命名；目录名就是文件扩展名 `jsonl`，见
tools/async_file_writer.py 的 _get_file_path），媒体文件（若开）写进
`{save_data_path}/{媒体目录名}/images|videos/{note_id}/`。

两个「不一致」都收在这个模块里，别处只见统一形态：

1. **字段名不一致** —— xhs 叫 note_id / comment_count，抖音叫 aweme_id，
   B 站的评论数叫 video_comment，知乎的点赞叫 voteup_count……归一化成
   id / title / desc / liked_count / comment_count / share_count / url 等
   统一字段（FIELD_MAP 是从 MC 各 store/*/__init__.py 的落盘字典逐个核实的）。

2. **目录名不一致** —— jsonl 用平台 CLI 短名（dy），媒体目录用长名
   （douyin）。MEDIA_DIR_NAMES 是这张对照表的唯一出处。

微博的本地图片是平铺的 `weibo/images/{picid}.jpg`（不带 note_id 目录），
无法关联到单条笔记，note 级的 local_images 只覆盖有目录约定的平台
（xhs / dy / bili），微博展示正文即可（它的 jsonl 里本来也没有图片 URL）。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.core.logging import get_logger
from app.models.crawl_job import CrawlPlatform

logger = get_logger(__name__)

#: jsonl / 媒体的相对目录规则（相对任务输出目录）。
#: jsonl 用 CLI 短名，媒体用各自 store 模块里的长名 —— 两列不同是 MC 的现状。
MEDIA_DIR_NAMES: Dict[str, str] = {
    CrawlPlatform.XHS: "xhs",
    CrawlPlatform.DY: "douyin",
    CrawlPlatform.KS: "kuaishou",
    CrawlPlatform.BILI: "bili",
    CrawlPlatform.WB: "weibo",
    CrawlPlatform.TIEBA: "tieba",
    CrawlPlatform.ZHIHU: "zhihu",
}

#: 图片/视频扩展名（本地媒体探测用，全小写带点）。
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm")

#: 各平台 jsonl 原生字段 → 统一字段。值为 None 表示该平台没有这项。
#: 从 MC 的 store/{platform}/__init__.py 落盘字典逐一核实，改字段名要两边同步。
FIELD_MAP: Dict[str, Dict[str, str]] = {
    CrawlPlatform.XHS: {
        "id": "note_id",
        "type": "type",
        "title": "title",
        "desc": "desc",
        "nickname": "nickname",
        "liked": "liked_count",
        "collected": "collected_count",
        "comments": "comment_count",
        "share": "share_count",
        "publish": "time",  # 毫秒时间戳
        "url": "note_url",
        "images": "image_list",  # 逗号分隔的 URL 串
        "keyword": "source_keyword",
    },
    CrawlPlatform.DY: {
        "id": "aweme_id",
        "type": "aweme_type",
        "title": "title",
        "desc": "desc",
        "nickname": "nickname",
        "liked": "liked_count",
        "collected": "collected_count",
        "comments": "comment_count",
        "share": "share_count",
        "publish": "create_time",
        "url": "aweme_url",
        "images": "note_download_url",  # 图文帖的图片 URL（逗号分隔）
        "cover": "cover_url",
        "keyword": "source_keyword",
    },
    CrawlPlatform.KS: {
        "id": "video_id",
        "type": "video_type",
        "title": "title",
        "desc": "desc",
        "nickname": "nickname",
        "liked": "liked_count",
        "collected": None,
        "comments": None,
        "share": None,
        "publish": "create_time",
        "url": "video_url",
        "cover": "video_cover_url",
        "keyword": "source_keyword",
    },
    CrawlPlatform.BILI: {
        "id": "video_id",
        "type": "video_type",
        "title": "title",
        "desc": "desc",
        "nickname": "nickname",
        "liked": "liked_count",
        "collected": "video_favorite_count",
        "comments": "video_comment",
        "share": "video_share_count",
        "publish": "create_time",
        "url": "video_url",
        "cover": "video_cover_url",
        "keyword": "source_keyword",
    },
    CrawlPlatform.WB: {
        "id": "note_id",
        "type": None,
        "title": None,  # 微博没有标题字段，正文即内容
        "desc": "content",
        "nickname": "nickname",
        "liked": "liked_count",
        "collected": None,
        "comments": "comments_count",
        "share": "shared_count",
        "publish": "create_time",
        "url": "note_url",
        "keyword": "source_keyword",
    },
    CrawlPlatform.TIEBA: {
        "id": "note_id",
        "type": None,
        "title": "title",
        "desc": "desc",
        "nickname": "user_nickname",
        "liked": None,
        "collected": None,
        "comments": "total_replay_num",
        "share": None,
        "publish": "publish_time",  # 字符串日期
        "url": "note_url",
        "keyword": "source_keyword",
    },
    CrawlPlatform.ZHIHU: {
        "id": "content_id",
        "type": "content_type",
        "title": "title",
        "desc": "desc",
        "text": "content_text",  # 知乎的正文在 content_text，desc 只是摘要
        "nickname": "user_nickname",
        "liked": "voteup_count",
        "collected": None,
        "comments": "comment_count",
        "share": None,
        "publish": "created_time",
        "url": "content_url",
        "keyword": "source_keyword",
    },
}

#: 本地媒体按 note_id 分目录的平台（微博平铺不在此列，见模块头注释）。
NOTE_MEDIA_PLATFORMS = (CrawlPlatform.XHS, CrawlPlatform.DY, CrawlPlatform.BILI)


def contents_files(output_dir: Path, platform: str) -> List[Path]:
    """列出任务输出目录里该平台的全部 contents jsonl（评论文件不含）。

    按**最后写入时间**排序而不是文件名：search 与 detail 是两个文件，
    字母序（detail < search）与写入先后（search 行先出、detail 行后补）
    正好相反，而 scan_notes 的去重语义是「保留后写的」。
    """
    pattern = output_dir / platform / "jsonl"
    try:
        files = list(pattern.glob("*_contents_*.jsonl"))
        files.sort(key=lambda path: (path.stat().st_mtime, path.name))
        return files
    except OSError:
        return []


def count_notes(output_dir: Path, platform: str) -> int:
    """数 contents jsonl 的总行数（runner 的进度与终态定稿都用它）。

    与 scan_notes 的去重口径不同：进度要的是「已产出多少行」这个实时数字，
    去重是结果展示层的事。坏行（空行/半行 JSON）不计。
    """
    total = 0
    for path in contents_files(output_dir, platform):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                total += sum(1 for line in handle if line.strip())
        except OSError:
            continue
    return total


def scan_notes(output_dir: Path, platform: str) -> List[dict]:
    """读出并归一化该平台全部 contents 行，按 id 去重（保留后写的）。

    MC 的 jsonl 落盘是 append：搜索结果先写一条、进详情后再写一条是常态，
    不去重的话结果列表里同一篇会出现两次。后写的字段更全（含互动数），
    所以保留后写的。
    """
    mapping = FIELD_MAP.get(platform)
    if mapping is None:
        return []

    raw_by_id: Dict[str, dict] = {}
    order: List[str] = []
    for path in contents_files(output_dir, platform):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(raw, dict):
                        continue
                    note_id = str(raw.get(mapping["id"]) or "")
                    if not note_id:
                        continue
                    if note_id not in raw_by_id:
                        order.append(note_id)
                    raw_by_id[note_id] = raw
        except OSError:
            continue

    return [normalize_note(raw_by_id[note_id], platform) for note_id in order]


def normalize_note(raw: dict, platform: str) -> dict:
    """把一条平台原生的 jsonl 行转成统一形态。"""
    mapping = FIELD_MAP[platform]

    def take(key: str) -> str:
        source = mapping.get(key)
        if not source:
            return ""
        value = raw.get(source)
        return "" if value is None else str(value)

    desc = take("desc")
    # 知乎正文优先 content_text（desc 只是摘要）；标题为空时用正文兜底
    if mapping.get("text"):
        desc = take("text") or desc
    title = take("title") or desc[:60]

    images = _split_urls(take("images"))
    return {
        "id": take("id"),
        "type": take("type"),
        "title": title,
        "desc": desc,
        "nickname": take("nickname"),
        "liked_count": take("liked"),
        "collected_count": take("collected"),
        "comment_count": take("comments"),
        "share_count": take("share"),
        "publish_time": _to_publish_time(raw.get(mapping["publish"])) if mapping.get("publish") else "",
        "url": take("url"),
        "cover": take("cover") or (images[0] if images else ""),
        "images": images,
        "source_keyword": take("keyword"),
    }


def collect_results(output_dir: Path, platform: str) -> List[dict]:
    """组装完整结果：归一化 + 附带本地媒体路径。

    local_images / local_videos 是相对任务输出目录的 posix 路径，
    前端拼 `/crawl/jobs/{id}/media/{path}` 取文件；
    local_image_dir 是图片所在目录的绝对路径，给「一键换背景」这类下游功能用
    （那边要的是「一个目录 + 一批文件名」，posix 相对路径换不过去）。
    """
    notes = scan_notes(output_dir, platform)
    for note in notes:
        images, videos, image_dir = local_media(output_dir, platform, str(note["id"]))
        note["local_images"] = images
        note["local_videos"] = videos
        note["local_image_dir"] = image_dir
    return notes


def local_media(
    output_dir: Path, platform: str, note_id: str
) -> Tuple[List[str], List[str], str]:
    """探测一条笔记已下载到本地的图片 / 视频文件。

    只对 NOTE_MEDIA_PLATFORMS 里的平台有意义；其它平台直接返回空
    （要么 MC 根本不下载媒体，要么像微博那样平铺无法按笔记关联）。

    Returns:
        (图片相对路径列表, 视频相对路径列表, 图片所在目录的绝对路径)。
        相对路径是 posix 风格、相对任务输出目录；目录路径是本机绝对路径，
        没有本地图片时给空串 —— 前端拿它当换背景的「原图目录」，
        把这一批图整批带过去。
    """
    if not note_id or platform not in NOTE_MEDIA_PLATFORMS:
        return [], [], ""

    media_dir = MEDIA_DIR_NAMES[platform]
    images: List[str] = []
    videos: List[str] = []
    for kind, sub, extensions, sink in (
        ("images", "images", IMAGE_EXTENSIONS, images),
        ("videos", "videos", VIDEO_EXTENSIONS, videos),
    ):
        folder = output_dir / media_dir / sub / note_id
        try:
            if not folder.is_dir():
                continue
            for item in sorted(folder.iterdir()):
                if item.is_file() and item.suffix.lower() in extensions:
                    sink.append(f"{media_dir}/{kind}/{note_id}/{item.name}")
        except OSError:
            continue
    # 与上面 loop 里的 images 分支同一个目录（平台下的 images/<笔记 id>），
    # 只是这里要的是绝对路径；有图才给，没图给空串 —— 前端据此禁用入口
    image_dir = output_dir / media_dir / "images" / note_id
    return images, videos, str(image_dir) if images else ""


def _split_urls(text: str) -> List[str]:
    """把 xhs/dy 的逗号分隔图片 URL 串拆成列表。"""
    return [part.strip() for part in text.split(",") if part.strip()]


def _to_publish_time(value) -> str:
    """发布时间统一成 ISO 字符串。

    xhs 的 time 是毫秒时间戳，多数平台是秒时间戳，贴吧是日期字符串 ——
    数字按量级判断单位，转不了（或本来就是字符串）就原样返回。这是个
    展示字段，不值得为它抛错。
    """
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.strip().isdigit()
    ):
        try:
            number = float(value)
            if number > 1e12:  # 毫秒
                number /= 1000.0
            if number <= 0:
                return str(value)
            return (
                datetime.fromtimestamp(number, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except (ValueError, OSError, OverflowError):
            return str(value)
    return str(value)


def media_file(output_dir: Path, relative: str) -> Optional[Path]:
    """把相对路径解析成输出目录内的绝对路径，越界返回 None。

    媒体服务接口的安全闸门：调用方（路由层）拿到的 relative 来自前端，
    resolve 后必须仍在任务输出目录内，否则就是一个任意文件读取漏洞。
    """
    base = output_dir.resolve()
    try:
        target = (base / relative).resolve()
        target.relative_to(base)
    except (ValueError, OSError):
        return None
    if not target.is_file():
        return None
    return target
