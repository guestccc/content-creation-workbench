"""一键成品的环境自检：AI 配置、ffmpeg 的 drawtext 能力、中文字体探测。

页面打开与「重新检测」按钮都走这里。与 crawler_env 不同，本模块**不做 TTL
缓存**：三项探测里最贵的是两次 ffmpeg 只读调用（-filters / -h filter=drawtext，
合计约 0.2–0.5 秒），而这个接口只在进页面和点「重新检测」时调 —— 缓存省不
下什么，反而会在用户刚改完 .env / 刚装完字体时给出过期结论。

探测原则与 subtitle_env / crawler_env 一致：**实测，不猜**。
drawtext 不是看 ffmpeg 版本号推断的，是 `-filters` 里真的列出它才算数；
boxborderw 参数是 drawtext 5.x 才加的，用 `-h filter=drawtext` 的 help 文本
实测，构建滤镜串时按这里探测到的能力决定带不带。
"""

import re
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.core.materials import FINALCUT, subdir
from app.services.ai_client import mask_key
from app.services.finalcut_render import DEFAULT_STYLE, style_catalog
from app.services.media_tools import find_tool, run_probe, verify_tool

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# AI 配置
# --------------------------------------------------------------------------


def probe_ai() -> dict:
    """AI（文案生成）这一项的自检结果。

    只查「配置齐不齐」，**不发真实请求**——环境自检不该花钱，也不该因为
    网络抖动把整个自检拖慢。真调通没调通，以用户点「生成文案」为准。

    Returns:
        {configured, ok, base_url, model, key_present, key_masked, detail, fix_hint}
        ok 与 configured 同义，分开留是给将来加「真实探活」留位置。
    """
    configured = bool(
        settings.AI_BASE_URL.strip()
        and settings.AI_MODEL.strip()
        and settings.AI_API_KEY.strip()
    )
    if configured:
        detail = f"已配置（{settings.AI_MODEL}，未实际调用）"
        fix_hint = ""
    else:
        detail = "未配置 API key，无法生成文案"
        fix_hint = "点页面右上角「AI 配置」填入 DeepSeek 的 API key（写回 backend/.env，长期生效）"
    return {
        "configured": configured,
        "ok": configured,
        "base_url": settings.AI_BASE_URL,
        "model": settings.AI_MODEL,
        "key_present": bool(settings.AI_API_KEY.strip()),
        "key_masked": mask_key(settings.AI_API_KEY),
        "detail": detail,
        "fix_hint": fix_hint,
    }


# --------------------------------------------------------------------------
# 中文字体探测
# --------------------------------------------------------------------------


class FontChoice(NamedTuple):
    """探测到的可用字体。file 是 drawtext 的 fontfile 要吃的绝对路径。"""

    file: str
    family: str


#: 候选字体表：按优先级排列，第一个存在的胜出。
#: Windows 的 msyh（微软雅黑）覆盖率与可读性最好；macOS 的 PingFang 同理；
#: Linux 各发行版路径不一，列常见几个。不够用时用 FINALCUT_FONT_FILE 指定。
_FONT_CANDIDATES = (
    (r"C:\Windows\Fonts\msyh.ttc", "微软雅黑"),
    (r"C:\Windows\Fonts\msyhbd.ttc", "微软雅黑 Bold"),
    (r"C:\Windows\Fonts\Deng.ttf", "等线"),
    (r"C:\Windows\Fonts\simhei.ttf", "黑体"),
    (r"C:\Windows\Fonts\simsun.ttc", "宋体"),
    ("/System/Library/Fonts/PingFang.ttc", "PingFang SC"),
    ("/System/Library/Fonts/STHeiti Light.ttc", "华文黑体"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "Noto Sans CJK"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf", "Noto Sans CJK SC"),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "文泉驿正黑"),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "文泉驿微米黑"),
)


def detect_font() -> Optional[FontChoice]:
    """找一个能渲染中文的字体文件。

    `FINALCUT_FONT_FILE` 显式指定优先（用户知道自己的机器上什么好看）；
    否则按候选表探测。找不到返回 None —— 没有中文字体，drawtext 会把文案
    渲成方块，这是「环境没就绪」而不是任务失败，由 probe_environment 报出来。
    """
    override = settings.FINALCUT_FONT_FILE.strip()
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            return FontChoice(file=str(path), family=f"自定义（{path.name}）")
        logger.warning("FINALCUT_FONT_FILE 指向的文件不存在：%s", override)
        return None

    for candidate, family in _FONT_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return FontChoice(file=str(path), family=family)
    return None


# --------------------------------------------------------------------------
# ffmpeg drawtext 能力
# --------------------------------------------------------------------------


#: 缺 drawtext 时的修复指引。写得具体是因为「装个 ffmpeg 就有」在这里不成立：
#: macOS 上 brew 的**普通 ffmpeg formula 不含 libfreetype**（实测 9.0.2 的
#: 编译配置里连 --enable-libfreetype 都没有），带 drawtext 的是另一个
#: keg-only 的 ffmpeg-full。早先这条写的是「macOS 用 brew install ffmpeg
#: （默认带）」，照着做完 drawtext 还是缺 —— 建议本身把人带进了死胡同。
_NEED_DRAWTEXT_HINT = (
    "换一个带 libfreetype 的构建：Windows 用 gyan.dev 的 full build；"
    "macOS 的普通 ffmpeg formula 不含 drawtext，要 brew install ffmpeg-full"
    "（keg-only，装完还需 brew link --overwrite ffmpeg-full）"
)


def probe_drawtext(ffmpeg_path: Optional[str] = None) -> dict:
    """实测 ffmpeg 的 drawtext 能力（不猜版本号）。

    Returns:
        {ok, path, version, has_drawtext, supports_boxborderw, detail, fix_hint}
        ok = 找到了真 ffmpeg 且 drawtext 滤镜在。supports_boxborderw 供
        构建滤镜串的一方决定要不要带 boxborderw（5.x 起支持）。
    """
    result = {
        "ok": False,
        "path": "",
        "version": "",
        "has_drawtext": False,
        "supports_boxborderw": False,
        "detail": "",
        "fix_hint": "",
    }

    path = ffmpeg_path or find_tool("ffmpeg")
    if not path:
        result["detail"] = "PATH 里找不到 ffmpeg"
        # 这里同样直接指向能用的构建：装一个不带 drawtext 的 ffmpeg 等于没装，
        # 烧字那一步照样跑不起来，不如一次说清楚。
        result["fix_hint"] = "安装 ffmpeg 并确保在 PATH 里。" + _NEED_DRAWTEXT_HINT
        return result
    result["path"] = path

    version = verify_tool("ffmpeg", path)
    if not version:
        result["detail"] = f"找到的 {path} 不是真的 ffmpeg（-version 自证失败）"
        result["fix_hint"] = "PATH 里有冒充 ffmpeg 的同名文件，请检查 PATH 顺序"
        return result
    result["version"] = version

    filters = run_probe([path, "-hide_banner", "-filters"], timeout=15)
    filters_text = (filters.stdout + filters.stderr) if filters else ""
    if not re.search(r"^\s*...?\s+drawtext\s", filters_text, re.MULTILINE):
        result["detail"] = "这个 ffmpeg 构建里没有 drawtext 滤镜（缺 libfreetype）"
        result["fix_hint"] = _NEED_DRAWTEXT_HINT
        return result
    result["has_drawtext"] = True

    help_result = run_probe([path, "-hide_banner", "-h", "filter=drawtext"], timeout=15)
    help_text = (help_result.stdout + help_result.stderr) if help_result else ""
    result["supports_boxborderw"] = "boxborderw" in help_text

    result["ok"] = True
    result["detail"] = f"ffmpeg {version}，drawtext 可用"
    return result


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------


def probe_environment(*, refresh: bool = False) -> dict:
    """组装一键成品的环境自检结果，直接给 `/finalcut/environment` 用。

    Args:
        refresh: 保留给 API 形状与其它模块一致；本模块不做缓存，两种调用等价。

    Returns:
        与 FinalcutEnvironmentResponse 字段一一对应的字典。**新增键时务必
        同步加到 schemas/finalcut_job.py 的同名模型上** —— FastAPI 的
        response_model 会把模型里没有的键静默丢掉。
    """
    del refresh  # 无缓存，参数只为与其它模块的接口形状一致
    ai = probe_ai()
    ffmpeg = probe_drawtext()
    font = detect_font()

    warnings: List[str] = []
    if font is None:
        warnings.append(
            "没有探测到中文字体：烧进画面的文案会变成方块。请安装中文字体，"
            "或在 backend/.env 里用 FINALCUT_FONT_FILE 指定一个字体文件。"
        )
    if ffmpeg["ok"] and not ffmpeg["supports_boxborderw"]:
        warnings.append(
            "这个 ffmpeg 的 drawtext 不支持 boxborderw（5.x 起才有）："
            "「白字黑边带底」样式的底色会紧贴文字，不影响使用，介意可升级 ffmpeg。"
        )

    return {
        "ready": bool(ai["ok"] and ffmpeg["ok"] and font is not None),
        "ai": ai,
        "ffmpeg": ffmpeg,
        "font": (
            {"file": font.file, "family": font.family} if font is not None else None
        ),
        "default_output_dir": str(subdir(FINALCUT)),
        "text_styles": style_catalog(),
        "default_style": DEFAULT_STYLE,
        "copy_count_default": settings.FINALCUT_COPY_COUNT_DEFAULT,
        "copy_count_max": settings.FINALCUT_COPY_COUNT_MAX,
        "max_items": settings.FINALCUT_MAX_ITEMS,
        "warnings": warnings,
    }
