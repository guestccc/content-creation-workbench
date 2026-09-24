"""素材抓取评论产物的读取、归一化与建树；以及补抓时的「指定形态」规则。

**评论文件与主内容是两个文件**：MC 把评论单独写进
`{输出目录}/{平台短名}/jsonl/{crawler_type}_comments_{YYYY-MM-DD}.jsonl`
（与 contents 同目录、不同文件），二级评论**平铺在同一份文件里**，靠
parent_comment_id 指回父评论。开关关掉或 max_comments=0 时这个文件**根本
不存在**（不是空文件）—— MC 的 jsonl writer 是按需 append 打开的。

为什么单独一个模块、不并进 crawl_results：那边的模块头注释写死了
「评论文件不含」（contents_files 的 glob 是 `*_contents_*`），那条边界是它
自洽的前提。而评论这边的差异比主内容还大 —— 微博的点赞叫
comment_like_count、贴吧/知乎的昵称叫 user_nickname、快手干脆既没有点赞也
没有父评论，外加一份主内容不需要的建树逻辑。混在一起两边都会变脏。

**关联字段复用 `crawl_results.FIELD_MAP[platform]["id"]`**：评论行里指回笔记的
那个字段（xhs/微博/贴吧 note_id、抖音 aweme_id、快手/B站 video_id、知乎
content_id）与主内容的 id 字段**逐平台完全一致**，另立一张表迟早对不上。
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import httpx

from app.core.logging import get_logger
from app.models.crawl_job import CrawlPlatform
from app.services import crawl_results

logger = get_logger(__name__)

#: 各平台评论 jsonl 的原生字段 → 统一字段；None 表示该平台没有这一项。
#: 从 MC 的 store/{platform}/__init__.py 与 model/m_*.py 的落盘结构逐一核实，
#: 改平台字段名要两边同步。
#:
#: - 微博的点赞是 comment_like_count（主内容那边才叫 liked_count）；
#: - 贴吧/知乎的昵称是 user_nickname（不是 nickname）；
#: - 贴吧的点赞与分享压根不落盘，快手同理且**连父评论都没有**
#:   （store/kuaishou/__init__.py 的 save_comment_item 里没有这两个键）——
#:   快手的评论层级是永久丢失的，只能全平铺，这不是本模块能补救的。
COMMENT_FIELD_MAP: Dict[str, Dict[str, Optional[str]]] = {
    CrawlPlatform.XHS: {
        "id": "comment_id",
        "content": "content",
        "nickname": "nickname",
        "liked": "like_count",
        "created": "create_time",  # 毫秒时间戳
        "parent": "parent_comment_id",
        "sub_count": "sub_comment_count",
        "pictures": "pictures",  # 逗号分隔的 URL 串
    },
    CrawlPlatform.DY: {
        "id": "comment_id",
        "content": "content",
        "nickname": "nickname",
        "liked": "like_count",
        "created": "create_time",
        "parent": "parent_comment_id",
        "sub_count": "sub_comment_count",
        "pictures": "pictures",
    },
    CrawlPlatform.KS: {
        "id": "comment_id",  # MC 落盘时可能是 None（store 里 str(comment_id) if comment_id else None）
        "content": "content",
        "nickname": "nickname",
        "liked": None,  # 快手不落盘点赞数
        "created": "create_time",
        "parent": None,  # 快手不落盘父评论 —— 层级丢失，全平铺
        "sub_count": "sub_comment_count",
        "pictures": None,
    },
    CrawlPlatform.BILI: {
        "id": "comment_id",
        "content": "content",
        "nickname": "nickname",
        "liked": "like_count",
        "created": "create_time",
        "parent": "parent_comment_id",
        "sub_count": "sub_comment_count",
        "pictures": None,
    },
    CrawlPlatform.WB: {
        "id": "comment_id",
        "content": "content",
        "nickname": "nickname",
        "liked": "comment_like_count",
        "created": "create_time",
        "parent": "parent_comment_id",  # 来自 MC 的 rootid，指向的是**根**评论
        "sub_count": "sub_comment_count",
        "pictures": None,
    },
    CrawlPlatform.TIEBA: {
        "id": "comment_id",
        "content": "content",
        "nickname": "user_nickname",
        "liked": None,  # 贴吧不落盘点赞数
        "created": "publish_time",  # 字符串日期
        "parent": "parent_comment_id",
        "sub_count": "sub_comment_count",
        "pictures": None,
    },
    CrawlPlatform.ZHIHU: {
        "id": "comment_id",
        "content": "content",
        "nickname": "user_nickname",
        "liked": "like_count",
        "created": "publish_time",  # 秒时间戳
        "parent": "parent_comment_id",
        "sub_count": "sub_comment_count",
        "pictures": None,
    },
}

#: 顶层评论的父 id 哨兵值。各平台**不统一**，这是实测结果不是猜的：
#: 抖音/B站给 "0"、小红书/微博/贴吧给空串、知乎给 None、微博偶尔是 "None"
#: 字符串。用真值判断会把 "0" 当成一个合法的父 id，那条评论就凭空消失了。
_TOP_LEVEL_PARENTS = frozenset({"", "0", "none"})

#: 建树深度上限：顶层算第 1 层。正常数据到 2 层就到底了（微博的 rootid 与
#: B 站的 parent 指的都是根），上限只用来防脏数据 —— 父链上有环时递归会转不出来。
MAX_DEPTH = 3

#: 父链回溯的硬上限，与 MAX_DEPTH 是两个用途：这个是防病态数据把 while 拖长。
_MAX_WALK = 32


def comments_files(output_dir: Path, platform: str) -> List[Path]:
    """列出任务输出目录里该平台的全部评论 jsonl（没有这个文件时返回空表）。

    排序与 contents_files 同一套口径、同一个理由：按**最后写入时间**排，
    因为 search 与 detail 是两个文件，字母序和真实写入先后正好相反，
    而下面的去重语义是「保留后写的」。
    """
    pattern = output_dir / platform / "jsonl"
    try:
        files = list(pattern.glob("*_comments_*.jsonl"))
        files.sort(key=lambda path: (path.stat().st_mtime, path.name))
        return files
    except OSError:
        return []


def scan_comments(output_dir: Path, platform: str, note_id: str) -> List[dict]:
    """读出该目录下属于这条笔记的评论（归一化 + 按去重键归并，保留后写的）。

    归一化后的节点带一个 parent_id 字段供建树用，group_comments 会把它摘掉，
    不往外露。
    """
    mapping = COMMENT_FIELD_MAP.get(platform)
    if mapping is None:
        return []
    rows = _read_comment_rows(output_dir, platform, note_id)
    return [normalize_comment(raw, platform) for raw in rows.values()]


def normalize_comment(raw: dict, platform: str) -> dict:
    """把一条平台原生的评论行转成统一形态（各平台的缺失项一律给空值）。"""
    mapping = COMMENT_FIELD_MAP[platform]

    def take(key: str) -> str:
        """取一个统一字段的原生值，转成字符串（None → 空串）。"""
        source = mapping.get(key)
        if not source:
            return ""
        value = raw.get(source)
        return "" if value is None else str(value)

    return {
        "id": _clean_id(take("id")),
        "content": take("content"),
        "nickname": take("nickname"),
        # 平台不落盘点赞时给空串：前端要能区分「0 个赞」和「这个平台没有赞」
        "liked_count": take("liked"),
        "created_at": crawl_results.to_publish_time(raw.get(mapping["created"]))
        if mapping.get("created")
        else "",
        "sub_comment_count": _to_int(take("sub_count")),
        "pictures": _split_pictures(take("pictures")),
        # 建树用，group_comments 消费后摘掉
        "parent_id": take("parent"),
        "orphan": False,
        "children": [],
    }


def group_comments(rows: List[dict], *, max_depth: int = MAX_DEPTH) -> List[dict]:
    """把平铺的评论行拼成树，返回顶层节点列表（子节点在各自的 children 里）。

    三个必须做对的地方：

    1. **顶层判定要认哨兵值**（见 _TOP_LEVEL_PARENTS）：各平台给的值不统一，
       把 "0" 当合法父 id 会让抖音/B站的顶层评论整条消失。
    2. **孤儿要提升到顶层并打标**：只抓到部分二级评论时（条数上限截断、风控），
       它的父不在这一批里，不提上来就整条看不见；但必须带 orphan=True ——
       让它长得像一级评论是在编造结构，前端要能说明「父评论未抓到」。
       父链上有环的同样按孤儿处理，不然递归会转不出来。
    3. **深度封顶**：超过 max_depth 的节点提升到顶层（保留内容、丢掉层级），
       宁可少一层也不要卡死。

    同一层内按时间**降序**（新的在前），时间认不出来的沉底并保持文件内原始
    顺序。每个父节点的子节点同理。
    """
    if not rows:
        return []

    nodes: List[dict] = [dict(row) for row in rows]

    # 按评论 id 索引，供父链回溯用。id 为空（快手可能落 None）的节点当不了父。
    index: Dict[str, dict] = {}
    for node in nodes:
        node_id = str(node.get("id") or "")
        if node_id and node_id not in index:
            index[node_id] = node

    roots: List[dict] = []
    for node in nodes:
        depth = _resolve_depth(node, index)
        if depth is None or depth <= 1 or depth > max_depth:
            # 孤儿（含成环）或超深：都提升到顶层。孤儿要打标，超深的不用 ——
            # 它的父确实抓到了，只是层级太深，谎报「父未抓到」反而更糟。
            if depth is None:
                node["orphan"] = True
            roots.append(node)
            continue
        parent = index.get(str(node.get("parent_id") or "").strip())
        if parent is not None:
            parent["children"].append(node)

    for node in nodes:
        node.pop("parent_id", None)

    roots = _sort_by_time(roots)
    for node in nodes:
        if node["children"]:
            node["children"] = _sort_by_time(node["children"])
    return roots


def collect_comments(
    output_dirs: Iterable[Path], platform: str, note_id: str
) -> List[dict]:
    """把多个输出目录里这条笔记的评论合并成一棵树。

    收多个目录是因为**补抓**：原任务抓过一次，派生任务可能又抓过几次，
    评论散在各自的目录里。合并语义与 contents_files 一致 —— **后写的覆盖
    先写的**（同一批评论被重抓时，后一次的数据更新），首次出现的顺序保留。
    """
    if platform not in COMMENT_FIELD_MAP:
        return []
    merged: Dict[str, dict] = {}
    for output_dir in output_dirs:
        merged.update(_read_comment_rows(Path(output_dir), platform, note_id))
    return group_comments([normalize_comment(raw, platform) for raw in merged.values()])


def count_comments(nodes: List[dict]) -> int:
    """数一棵评论树的节点总数（含各级子评论）。"""
    return sum(1 + count_comments(node.get("children") or []) for node in nodes)


# ---------------------------------------------------------------------------
# 评论图片本地化（懒下载缓存）
#
# MC 只下载**笔记**的图（ENABLE_GET_MEIDAS），评论里的图从来不落盘，而平台
# 图床的 URL 是**带时效签名的**（小红书路径里那两段就是签发时间戳 + MD5 签名，
# 实测几天就过期 403）。透传 URL 注定裂图，所以读评论时把图片就地下载缓存：
#
# - 缓存放在**根任务**输出目录的 `{platform}/comment_media/` 下 —— 与 jsonl
#   同级的一个新子目录（MC 不认识它，互不干扰），删除任务时随目录一起清掉；
# - 前端拿到的 pictures 从 URL 换成相对路径，走已有的 /media 接口取文件；
# - 下载失败的 URL 原样透传：4xx（签名过期）是救不回来的，写一个 .skip 侧车
#   标记不再重试 —— 弹窗轮询期间每 5 秒读一次，不标记会反复重打几百个 403；
#   网络错误 / 5xx 不标记，下一轮还有机会；
# - 补抓拿到的新 URL 签名不同、哈希也不同，天然会重新下载 —— 所以「过期图
#   点再抓一次」就是完整的恢复路径。
# ---------------------------------------------------------------------------

#: 评论图片缓存目录名（位于输出目录的 {platform}/ 下）
COMMENT_MEDIA_DIR = "comment_media"

#: 懒下载的预算：单张超时、单次调用总预算、单次最多下载张数、单张大小上限。
#: 读评论是 GET，不能被下载拖死 —— 超预算的 URL 本轮保持原样，下一轮再补。
PICTURE_TIMEOUT_SECONDS = 5.0
PICTURE_BUDGET_SECONDS = 8.0
PICTURE_MAX_BATCH = 30
PICTURE_MAX_BYTES = 10 * 1024 * 1024

#: Content-Type → 文件后缀（FileResponse 靠后缀回 Content-Type，浏览器才能渲染）
_PICTURE_EXTS = {
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/avif": ".avif",
}


def localize_pictures(output_dir: Path, platform: str, nodes: List[dict]) -> None:
    """就地把评论树里的图片 URL 换成本地缓存路径（下不到的保持 URL 不动）。

    幂等：已缓存的 URL 直接命中文件；被 .skip 标记的不再发请求；不是
    http(s) 的值（理论上不会出现）原样保留。预算按「先到先得」消耗 ——
    树序（顶层时间倒序）遍历，排前面的评论先把预算用掉，剩下的下一轮
    轮询 / 重开弹窗时再补。
    """
    if platform not in COMMENT_FIELD_MAP or not nodes:
        return
    cache_dir = output_dir / platform / COMMENT_MEDIA_DIR

    resolved: Dict[str, Optional[str]] = {}
    attempts = 0
    started = time.monotonic()

    def resolve(url: str) -> Optional[str]:
        """一个 URL → 本地相对路径；None 表示这轮拿不到（保持 URL）。"""
        if not url.startswith(("http://", "https://")):
            return url
        if url in resolved:
            return resolved[url]
        hit, skipped = _cached_or_skipped(cache_dir, url)
        if hit is not None:
            resolved[url] = hit
            return hit
        if skipped:
            # 已知不可取（上次 4xx）：直接放行，**不消耗配额** —— 否则图上
            # 几十条的笔记里，过期 URL 每轮都会把批次上限吃光，新抓到的 URL
            # 永远轮不到下载
            resolved[url] = None
            return None
        nonlocal attempts
        if (
            attempts >= PICTURE_MAX_BATCH
            or time.monotonic() - started >= PICTURE_BUDGET_SECONDS
        ):
            resolved[url] = None
            return None
        attempts += 1
        resolved[url] = _download_picture(cache_dir, platform, url)
        return resolved[url]

    def visit(node: dict) -> None:
        pictures = node.get("pictures") or []
        if pictures:
            node["pictures"] = [resolve(url) or url for url in pictures]
        for child in node.get("children") or []:
            visit(child)

    for node in nodes:
        visit(node)


# ---------------------------------------------------------------------------
# 补抓：喂给 --specified_id 的形态逐平台不同
#
# 读 MC 的 cmd_arg/arg.py 与各 media_platform/*/core.py 核实：绝大多数平台的
# `*_SPECIFIED_NOTE_URL_LIST` / `video_url` / `full_note_url` 收的是**链接**，
# 唯独微博的 detail 分支直接 `get_note_info_task(note_id=note_id)`，喂链接会坏。
# ---------------------------------------------------------------------------

#: 收链接、且落盘的链接能原样喂回去的平台。
_URL_SPEC_PLATFORMS = (
    CrawlPlatform.XHS,
    CrawlPlatform.DY,
    CrawlPlatform.KS,
    CrawlPlatform.TIEBA,
    CrawlPlatform.ZHIHU,
)

#: 知乎 detail 分支认的三种页面地址（复刻 MC 自己的 judge_zhihu_url，
#: media_platform/zhihu/help.py）：认不出的地址它会记一行 "not found" 后跳过，
#: 任务以 0 条产物判失败。
_ZHIHU_PAGE_MARKERS = ("/answer/", "/p/", "/zvideo/")


def refetch_spec(platform: str, note: dict) -> Optional[str]:
    """这条笔记补抓时喂给 `--specified_id` 的值；补不了返回 None。

    补不了的原因由 refetch_unsupported_reason 给文案 —— 两个函数分开是为了让
    「能不能补」是纯函数可单测的，而文案改动不必带测试。

    Args:
        platform: 平台短名。
        note: crawl_results 归一化后的笔记（只读 id 与 url）。
    """
    if platform == CrawlPlatform.BILI:
        # 落盘的是 av 号，而 MC 的解析器只认 BV（见 refetch_unsupported_reason）
        return None
    if platform == CrawlPlatform.WB:
        note_id = _clean_id(str(note.get("id") or ""))
        return note_id or None
    if platform not in _URL_SPEC_PLATFORMS:
        return None

    url = str(note.get("url") or "").strip()
    if not url:
        return None
    if platform == CrawlPlatform.XHS and not _xhs_url_has_token(url):
        return None
    if platform == CrawlPlatform.ZHIHU and not _is_zhihu_page_url(url):
        return None
    return url


def refetch_unsupported_reason(platform: str, note: dict) -> str:
    """不能补抓时给用户看的一句话原因；能补抓时返回空串。"""
    if refetch_spec(platform, note) is not None:
        return ""

    if platform == CrawlPlatform.BILI:
        return (
            "B 站暂不支持补抓：抓取结果里存的是 av 号，而 MediaCrawler 只认 BV 号，"
            "重新抓会一条都拿不到。"
        )
    if platform == CrawlPlatform.XHS:
        return (
            "这条笔记的链接缺少小红书访问令牌（xsec_token），补抓不到数据。"
            "请重新搜索这条笔记、用新链接建一次抓取任务。"
        )
    if platform == CrawlPlatform.ZHIHU:
        return (
            "这条内容的链接不是知乎的回答 / 文章 / 视频页面地址（视频类有时只存了"
            "CDN 直链），补抓不到数据。"
        )
    if platform == CrawlPlatform.WB:
        return "这条微博没有可用的数字 ID，无法补抓。"
    return "这条笔记缺少可用的原文链接，无法补抓。"


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _read_comment_rows(
    output_dir: Path, platform: str, note_id: str
) -> Dict[str, dict]:
    """读出该目录下这条笔记的评论原始行，按去重键归并（保留后写的）。

    返回的是有序 dict：键首次出现的顺序 = 文件内的原始顺序，后来的同键行
    覆盖值但不改位置 —— 这正是「保留后写的、顺序按首次出现」想要的语义。
    """
    mapping = COMMENT_FIELD_MAP.get(platform)
    note_field = (crawl_results.FIELD_MAP.get(platform) or {}).get("id")
    if mapping is None or not note_field or not note_id:
        return {}

    rows: Dict[str, dict] = {}
    for path in comments_files(output_dir, platform):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        continue  # 半行 JSON（进程被杀时常见），跳过
                    if not isinstance(raw, dict):
                        continue
                    # 只收这条笔记的行：一份 comments 文件里可能有多个笔记的评论
                    if str(raw.get(note_field) or "") != note_id:
                        continue
                    rows[_comment_key(raw, mapping)] = raw
        except OSError:
            continue
    return rows


def _comment_key(raw: dict, mapping: dict) -> str:
    """一条评论的去重键。

    id 拿不到时要退到内容指纹，不能拿空串当键：快手的 comment_id 可能是
    None、微博缺字段时落的是**字符串 "None"**（store/weibo/__init__.py 里
    直接 str(comment_item.get("id"))）—— 两者都不能当合法 id 用，否则所有
    缺 id 的评论会被归并成一条，评论数直接少一大截。
    """
    raw_id = _clean_id(str(raw.get(mapping.get("id") or "") or ""))
    if raw_id:
        return f"id:{raw_id}"
    parts = [raw.get(mapping.get(name) or "") for name in ("content", "nickname", "created")]
    return "fp:" + "|".join("" if part is None else str(part) for part in parts)


def _clean_id(text: str) -> str:
    """规范化一个 id：去空白，并把字符串 "None"/"null" 当成没有 id。

    MC 多处是 `str(x.get(...))` 直接落盘，字段缺失时存进去的就是字面的
    "None" —— 它看着像个合法 id，当成有效值会串起错误的父子关系。
    """
    cleaned = text.strip()
    if cleaned.lower() in ("none", "null"):
        return ""
    return cleaned


def _resolve_depth(node: dict, index: Dict[str, dict]) -> Optional[int]:
    """节点在树里的层级（顶层 = 1）；父不在本批、父链成环、或链长病态时返回 None。

    回溯父链而不是从顶层往下递归：评论总量不大（单条笔记最多几百条），
    而上限 MAX_DEPTH 很小，回溯天然就能同时解决「深度封顶」与「环检测」
    两件事，不需要额外维护一个 visited 集合。
    """
    depth = 1
    seen = {id(node)}
    current = node
    while True:
        parent_id = str(current.get("parent_id") or "").strip()
        if _is_top_level(parent_id):
            return depth
        parent = index.get(parent_id)
        if parent is None or id(parent) in seen:
            return None  # 父不在本批（截断/风控）或父链成环
        seen.add(id(parent))
        current = parent
        depth += 1
        if depth > _MAX_WALK:
            return None


def _is_top_level(parent_id: str) -> bool:
    """父 id 是不是「没有父」的哨兵值。"""
    return _clean_id(parent_id).lower() in _TOP_LEVEL_PARENTS


def _sort_by_time(nodes: List[dict]) -> List[dict]:
    """按时间倒序；时间认不出来的沉底并保持原有顺序。

    直接比归一化后的字符串就够：同一条笔记的评论来自同一个平台、格式同构
    （to_publish_time 认得出的给 ISO，认不出的原样返回）。判据是「还在
    时间形状里」，而不是猜哪些字符串能解析 —— 猜错了会把一整批评论沉底。
    """
    known: List[dict] = []
    unknown: List[dict] = []
    for node in nodes:
        text = str(node.get("created_at") or "")
        (known if _looks_like_time(text) else unknown).append(node)
    # 稳定排序：时间一样的保持文件内原始顺序
    known.sort(key=lambda node: str(node.get("created_at") or ""), reverse=True)
    return known + unknown


def _looks_like_time(text: str) -> bool:
    """形如 2024-03-05T12:00:00Z 或 2024-03-05 12:00 的字符串。"""
    return len(text) >= 10 and text[:4].isdigit() and text[4] == "-"


def _to_int(text: str) -> int:
    """子评论数转 int；拿不到（空串、非数字）按 0 —— 它只用于展示「还有几条没抓到」。"""
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _split_pictures(text: str) -> List[str]:
    """评论图片：xhs/dy 落的是逗号分隔的 URL 串，其余平台为空。"""
    return [part.strip() for part in text.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# 评论图片本地化的内部实现
# ---------------------------------------------------------------------------

#: 下载用的 UA：平台图床普遍不校验 Referer（前端 no-referrer 一直能加载），
#: 但一个空 UA 可能被边缘节点直接拒掉，带上浏览器样子的最稳。
_PICTURE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _picture_key(url: str) -> str:
    """URL → 缓存文件名主体（sha1 前 20 位）。

    同一张图的签名 URL 每次抓取都不同，所以键是**整条 URL**：新签名来了
    天然算新文件重新下载 —— 这正是「过期图补抓一次就好」的机制。
    """
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]


def _cached_or_skipped(cache_dir: Path, url: str) -> Tuple[Optional[str], bool]:
    """查这张图的缓存状态：返回 (已缓存的相对路径, 是否已被标记不可取)。

    两个状态是**互斥**的：下到了就是缓存文件，没下到（4xx）才是 .skip 标记。
    分开返回是因为两者的处理不同 —— 缓存命中直接用，skip 命中要跳过且不能
    消耗下载配额（见 localize_pictures 里的注释）。
    """
    key = _picture_key(url)
    try:
        matches = list(cache_dir.glob(f"{key}.*"))
    except OSError:
        return None, False
    cached = [path for path in matches if path.suffix != ".skip"]
    if cached:
        return _relative_media_path(cached[0]), False
    return None, any(path.suffix == ".skip" for path in matches)


def _relative_media_path(path: Path) -> str:
    """缓存文件 → 相对输出目录根的 posix 路径（/media 接口的寻址口径）。"""
    # cache_dir 是 {output_dir}/{platform}/comment_media，文件往上三级就是根
    return "/".join(path.parts[-3:])


def _download_picture(cache_dir: Path, platform: str, url: str) -> Optional[str]:
    """下载一张评论图并落盘缓存；失败返回 None（URL 原样透传）。

    4xx 写 .skip 侧车标记：签名过期 / 防盗链是救不回来的，而弹窗轮询期间
    这个接口每 5 秒被读一次，不标记会反复重打同一批 403。网络错误与 5xx
    不标记 —— 那可能是本地抖动，下一轮还有机会。

    调用方负责**先查**这个标记（_cached_or_skipped）：本函数只管「打一次
    网络、按结果落盘或标记」，不重复判断。
    """
    key = _picture_key(url)
    try:
        status, body, content_type = _http_get_picture(url)
    except Exception:  # noqa: BLE001 - 单张失败不能拖垮整次读取
        logger.warning("评论图片下载失败（网络异常）| %s", url)
        return None

    if 400 <= status < 500:
        _write_marker(cache_dir / f"{key}.skip")
        logger.info("评论图片不可取（%s，已标记跳过）| %s", status, url)
        return None
    if status < 200 or status >= 300 or not body or len(body) > PICTURE_MAX_BYTES:
        logger.warning("评论图片下载异常 | status=%s | bytes=%s | %s", status, len(body), url)
        return None

    ext = _PICTURE_EXTS.get(content_type.split(";")[0].strip().lower(), ".jpg")
    target = cache_dir / f"{key}{ext}"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：两个并发请求缓存同一张图时互不踩踏
        tmp = cache_dir / f"{key}{ext}.tmp"
        tmp.write_bytes(body)
        os.replace(tmp, target)
    except OSError:
        logger.warning("评论图片写盘失败 | %s", target)
        return None
    return f"{platform}/{COMMENT_MEDIA_DIR}/{target.name}"


def _http_get_picture(url: str) -> Tuple[int, bytes, str]:
    """发一次图片 GET，返回 (状态码, 响应体, Content-Type)。

    单独抽出来是为了测试能打桩（不发真网络请求）；网络层异常原样抛给调用方。
    """
    with httpx.Client(
        timeout=PICTURE_TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": _PICTURE_UA}
    ) as client:
        response = client.get(url)
        return response.status_code, response.content, response.headers.get("content-type", "")


def _write_marker(path: Path) -> None:
    """写一个空的侧车标记文件（失败也要写盘成功才有意义，写不进就算了）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    except OSError:
        logger.warning("评论图片跳过标记写盘失败 | %s", path)


def _xhs_url_has_token(url: str) -> bool:
    """小红书详情必须带 xsec_token，且不能是字面的 "None"。

    MC 落盘时是 f-string 直接插值（store/xhs/__init__.py）：字段缺失时拼出来
    的链接长这样 `...?xsec_token=None&xsec_source=pc_search` —— 它照样是个
    能解析的 URL，只检查「参数在不在」会放一个必然 0 结果的补抓任务进队列。
    """
    token = (parse_qs(urlsplit(url).query).get("xsec_token") or [""])[0].strip()
    return bool(token) and token.lower() != "none"


def _is_zhihu_page_url(url: str) -> bool:
    """是不是知乎的回答 / 文章 / 视频页面地址（复刻 MC 的 judge_zhihu_url 判据）。

    视频类内容的 content_url 有时是 CDN 直链（media_platform/zhihu/help.py 的
    `_extract_zvideo_content` 有两个分支），那种地址 MC 认不出来会直接跳过。
    """
    return any(marker in url for marker in _ZHIHU_PAGE_MARKERS)
