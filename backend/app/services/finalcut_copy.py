"""一键成品的文案生成纯逻辑：字幕拆解、字数预算、提示词构造、产物解析。

这一层不碰数据库、不碰 HTTP（调 AI 的是 runner），全部是可以单测的纯函数。

核心换算：**文案全程上屏，所以文案总字数必须能在视频时长内读完**。
`char_budget` 用「每秒上屏字数」（FINALCUT_CHARS_PER_SECOND，带货口播约 4-5
字/秒）把视频时长换算成字数预算，提示词把这个预算明确写给模型。给用户的
`char_count` 由解析侧 `count_chars` 实测 —— LLM 数不准自己的字数（真机上
92 字的文案回填 135），采信它「这条多少字」就成了误导。
"""

import re
from typing import Dict, List, Optional, Tuple

from app.core.config import settings
from app.schemas.finalcut_job import (
    CopyAnalysis,
    CopyBreakdownPart,
    CopyCandidate,
    CopyResultPayload,
)
from app.services.ai_client import extract_json_object

#: SRT 的序号行（纯数字）。
_SRT_INDEX_RE = re.compile(r"^\s*\d+\s*$")
#: SRT/VTT 的时间轴行（00:00:01,000 --> 00:00:03,000 或点号分隔）。
_TIMESTAMP_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}\s*-->\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}"
)
#: ASS 的对话行：Dialogue: 0,0:00:01.00,...,Default,,0,0,0,,文本（第 9 个逗号后是文本）。
_ASS_DIALOGUE_RE = re.compile(r"^Dialogue:\s*[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,(.*)$")


def srt_to_material(text: str, max_chars: Optional[int] = None) -> Tuple[str, bool]:
    """把字幕文件内容拆解成喂给 AI 的纯文案素材。

    做的事：剔除序号行与时间轴行、剥 VTT 的 WEBVTT 头与 ASS 的 Dialogue
    包装、去掉 ASS 的行内特效标签（`{\\...}`）、合并连续重复行（双语/双行
    字幕常有相邻重复）、折叠空行。ASS 的 `[Script Info]`/`[V4+ Styles]` 等
    非 Events 段整段跳过（那些键值行是元数据，不是口播内容）。

    Args:
        text: 字幕文件内容（.srt / .ass / .vtt 都走这里）。
        max_chars: 素材字数上限（默认 FINALCUT_SRT_MAX_CHARS）。超出截断，
            截断处补一行省略标记，让模型知道素材不完整。

    Returns:
        (素材文本, 是否被截断)。空文件返回 ("", False)。
    """
    if max_chars is None:
        max_chars = settings.FINALCUT_SRT_MAX_CHARS

    lines: List[str] = []
    ass_section: Optional[str] = None  # 遇到 [段头] 后记录当前 ASS 段名
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper() == "WEBVTT":
            continue
        if line.startswith("["):
            ass_section = line.strip("[]").strip().lower()
            continue
        if ass_section is not None and ass_section != "events":
            # [Script Info] 的 Title:、[V4+ Styles] 的 Style: 之类都不是素材
            continue
        if _SRT_INDEX_RE.match(line) or _TIMESTAMP_RE.match(line):
            continue
        dialogue = _ASS_DIALOGUE_RE.match(line)
        if dialogue is not None:
            line = dialogue.group(1).strip()
        elif line.startswith(("Dialogue:", "Comment:", "NOTE", "Format:")):
            # ASS 的其它结构行与 VTT 的 NOTE 注释行，对文案没有价值
            continue
        # ASS 行内特效标签（{\an8} 之类）与换行标记 \N
        line = re.sub(r"\{[^}]*\}", "", line).replace("\\N", " ").replace("\\n", " ")
        line = line.strip()
        if not line:
            continue
        # 合并连续重复行：双行重叠字幕会让同一句话出现两遍，喂给模型是噪音
        if lines and lines[-1] == line:
            continue
        lines.append(line)

    material = "\n".join(lines)
    if len(material) <= max_chars:
        return material, False
    return material[:max_chars].rstrip() + "\n（素材过长，后续内容已省略）", True


def char_budget(duration_seconds: float) -> Tuple[int, int]:
    """视频时长 → 上屏字数预算（下限, 上限）。

    文案全程显示，观众要在 `duration` 秒内读完它：预算 = 时长 × 每秒字数，
    下限取 0.7 倍（给节奏慢的口播留余量）。例：15 秒 × 4.5 ≈ 47–68 字。
    """
    cps = settings.FINALCUT_CHARS_PER_SECOND
    mid = max(0.0, duration_seconds) * cps
    low = max(10, round(mid * 0.7))
    high = max(low + 5, round(mid))
    return low, high


#: 内置 system 提示词。AI_SYSTEM_PROMPT 配置可整体替换它（留给会写提示词的人）。
#: 注意：DeepSeek 开 response_format=json_object 时要求提示词里出现「JSON」。
def count_chars(text: str) -> int:
    """上屏字数：含标点、不含换行与空白。

    服务端实测值。提示词里仍要求模型回填 char_count（让它对预算有自觉），
    但落库一律用这个函数重算 —— 真机上模型把 92 字的文案回填成 135，
    采信它，页面上「字数与时长配不配」的核对就成了误导。
    """
    return sum(1 for ch in text if not ch.isspace())


_DEFAULT_SYSTEM_PROMPT = """你是一位资深的短视频带货文案策划。用户会给你一段视频的字幕素材、视频时长和字数预算，你要：

1. 先拆解素材：主题是什么、说给谁听、核心卖点有哪些、整体调性是什么；
2. 再基于拆解，写出若干条**可以直接烧在视频画面上全程展示**的广告文案。

硬性要求：
- 只输出一个 JSON 对象，不要输出任何其它文字、解释或 markdown 围栏；
- 每条文案的总字数必须落在给定的字数预算内（文案会全程显示在画面上，观众要能在视频时长内读完）；
- 文案分 1-3 行，每行尽量不超过 15 个字，用 \\n 换行；
- 每条文案要有一个明确的切入角度，角度之间互不重复（如：痛点开场、效果对比、价格锚点、场景代入、行动号召）；
- 不要使用表情符号（emoji 在烧录时会变成方块）。

JSON 的顶层结构（字段名必须逐字一致）：
{
  "analysis": {
    "topic": "素材主题（一句话）",
    "audience": "目标受众（一句话）",
    "selling_points": ["核心卖点1", "核心卖点2"],
    "tone": "调性（如：紧迫促销 / 闺蜜安利 / 专业测评）"
  },
  "copies": [
    {
      "text": "文案正文（用 \\n 分行）",
      "angle": "切入角度",
      "target_seconds": 15,
      "char_count": 34,
      "why": "为什么这么写：结合素材拆解说明这条文案的策略（2-3 句）",
      "highlights": ["好在哪里：要点1", "要点2"],
      "breakdown": [
        {"part": "段落名（如 开头钩子）", "content": "该段原文", "explain": "这段的作用"}
      ]
    }
  ]
}"""


def build_copy_messages(
    material: str,
    duration_seconds: float,
    hint: str,
    count: int,
    budget: Tuple[int, int],
) -> List[Dict[str, str]]:
    """构造 chat/completions 的 messages（system + user）。

    user 消息把四件事说死：视频时长、字数预算、要几条、素材正文。
    模型回填的 target_seconds 供用户核对「字数与时长配不配」；
    char_count 不信模型（它数不准），解析侧用 count_chars 实测覆盖。
    """
    system = settings.AI_SYSTEM_PROMPT.strip() or _DEFAULT_SYSTEM_PROMPT
    low, high = budget

    user_parts = [
        f"视频时长：{duration_seconds:.1f} 秒",
        f"字数预算：每条文案 {low}–{high} 字（含标点，不含换行）",
        f"生成条数：{count} 条候选文案",
    ]
    if hint:
        user_parts.append(f"补充要求：{hint}")
    user_parts.append(f"\n字幕素材：\n{material}")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def parse_copy_payload(raw: str, *, max_copies: Optional[int] = None) -> Tuple[dict, List[dict]]:
    """把 AI 返回的原文解析成 (analysis, copies)，逐条校验。

    容错策略：extract_json_object 先剥围栏/取花括号；之后用 pydantic 逐条
    校验 copies，**缺字段的条目丢掉**（字段都有默认值兜底），正文为空才丢；
    一条都不剩才算这轮生成失败。条数上限截断（模型有时多给）。

    Returns:
        (analysis dict, copies list[dict])——与 CopyResultPayload 同形状的可落库数据。

    Raises:
        ValueError: 原文不是 JSON、或一条有效文案都没有。
    """
    if max_copies is None:
        max_copies = settings.FINALCUT_COPY_COUNT_MAX

    payload = extract_json_object(raw)  # 坏 JSON 抛 AiError(bad_response)，原样上传
    result = CopyResultPayload(
        analysis=CopyAnalysis(**(payload.get("analysis") or {}))
        if isinstance(payload.get("analysis") or {}, dict)
        else CopyAnalysis(),
        copies=[],
    )

    copies: List[CopyCandidate] = []
    raw_copies = payload.get("copies")
    if isinstance(raw_copies, list):
        for entry in raw_copies:
            if not isinstance(entry, dict):
                continue
            try:
                text = str(entry.get("text") or "").strip()
                candidate = CopyCandidate(
                    text=text,
                    angle=str(entry.get("angle") or "")[:100],
                    target_seconds=float(entry.get("target_seconds") or 0),
                    # 模型回填的 char_count 数不准（真机偏差 40%+），一律实测
                    char_count=count_chars(text),
                    why=str(entry.get("why") or ""),
                    highlights=[
                        str(h) for h in (entry.get("highlights") or []) if str(h).strip()
                    ][:10],
                    breakdown=[
                        CopyBreakdownPart(
                            part=str(b.get("part") or "")[:50],
                            content=str(b.get("content") or "")[:500],
                            explain=str(b.get("explain") or "")[:500],
                        )
                        for b in (entry.get("breakdown") or [])
                        if isinstance(b, dict)
                    ][:10],
                )
            except (TypeError, ValueError):
                continue
            if not candidate.text:
                continue
            copies.append(candidate)
            if len(copies) >= max_copies:
                break

    if not copies:
        raise ValueError("AI 返回里没有一条可用的文案（原文已存进任务的 raw_response）")

    result.copies = copies
    return result.analysis.model_dump(), [c.model_dump() for c in result.copies]
