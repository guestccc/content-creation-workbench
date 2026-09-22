"""一键成品的文案生成纯逻辑：字幕拆解、字数预算、提示词构造、产物解析。

这一层不碰数据库、不碰 HTTP（调 AI 的是 runner），全部是可以单测的纯函数。

核心换算：**文案是口播稿（配音稿），不是字幕**。用户拿它去剪映生成 TTS 配音、
同时人工烧成字幕，所以一条文案必须**念满视频时长** —— 短了视频后半段没声音。

字数预算因此是「时长 × 口播语速」（字/秒）。
语速**不是写死的常量，而是用户实测校准出来的值**：拿一段已知字数的文案在剪映里
生成配音、量出实际秒数（87 字念了 15 秒 → 5.8 字/秒），填进页面即可，读写见
services/finalcut_settings.py。默认值见 config.default_chars_per_second()。

语速**按任务区分**（有的配音要读快、有的要读慢）：创建任务时把当时生效的语速
快照进 `FinalcutCopyJob.chars_per_second`，`char_budget` 由调用方传进来 ——
本模块不读全局配置。全局值只是**新任务的默认值**，事后改它不会动到老任务
（否则历史任务的预算与页面上算出的秒数会跟当时生成的内容对不上）。

给用户的 `char_count` 由解析侧 `count_chars` 实测 —— LLM 数不准自己的字数
（真机上 92 字的文案回填 135），采信它「这条多少字」就成了误导。
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


#: 字数预算的容差与保底。目标字数 = 时长 × 语速，预算是它 ±10%（见 char_budget）。
_MIN_BUDGET_LOW = 10  # 预算下限（短视频也要够写一句话）
_MIN_BUDGET_SPAN = 5  # 区间最小跨度（低=高 时模型没有腾挪空间）
_BUDGET_TOLERANCE = 0.1  # ±10%

#: 分段骨架：(段落名, 占目标字数的比例)，各比例之和 = 1.0。
_SEGMENT_PLAN = (
    ("开头钩子（一句话抓住人）", 0.15),
    ("痛点或场景（说中观众自己的处境）", 0.25),
    ("卖点与证据（凭什么值得买，2-3 个具体的点）", 0.375),
    ("价格与行动号召（怎么买、为什么现在买）", 0.225),
)
#: 目标字数低于这个值时改用三段骨架：四段每段只剩十几字，模型写不出东西。
_SHORT_COPY_TARGET = 120
_SHORT_SEGMENT_PLAN = (
    ("开头钩子（一句话抓住人）", 0.30),
    ("卖点与证据（凭什么值得买）", 0.45),
    ("价格与行动号召（怎么买）", 0.25),
)
#: 每段给模型的区间 = 该段配额 ±5%；各段之和必然落在整体预算内。
_SEGMENT_SPREAD = 0.05


def char_budget(duration_seconds: float, chars_per_second: float) -> Tuple[int, int]:
    """视频时长 + 口播语速 → 口播稿字数预算（下限, 上限）。

    目标字数 = 时长 × 语速。语速由调用方给（runner 传任务上的快照，
    页面预览传输入框里的值）—— 本函数不读全局配置，见模块 docstring。
    区间取 ±10%：实测语速本身有波动（同一段话两次配音差个几秒很正常），
    卡死一个数会让模型为了凑字数硬塞废话。

    预算极小时按比例缩会缩到没法写（10 秒 × 1.0 字/秒 = 9–11 字，不够铺垫），
    所以下限抬到 _MIN_BUDGET_LOW、跨度至少 _MIN_BUDGET_SPAN。

    例：36.3 秒 × 5.8 字/秒 ≈ 210 字 → (189, 232)；15 秒 × 5.0 → (68, 82)。
    """
    target = max(0.0, duration_seconds) * chars_per_second
    low = max(_MIN_BUDGET_LOW, round(target * (1 - _BUDGET_TOLERANCE)))
    high = max(low + _MIN_BUDGET_SPAN, round(target * (1 + _BUDGET_TOLERANCE)))
    return low, high


def _segment_plan(target_chars: int) -> List[Tuple[str, int, int]]:
    """把目标字数拆成几段，返回 [(段落名, 该段下限, 该段上限), ...]。

    为什么由后端拆而不是让模型自己分配：模型给自己分字数时几乎没有一次分得准
    （分完就忘），而后端算出来的各段之和恒等于目标 ±5%，模型只要照着每段写，
    总数自然落进预算 —— 这是把「控制总量」降级成「控制四小段」的手段。
    """
    plan = _SHORT_SEGMENT_PLAN if target_chars < _SHORT_COPY_TARGET else _SEGMENT_PLAN
    return [
        (
            name,
            round(target_chars * ratio * (1 - _SEGMENT_SPREAD)),
            round(target_chars * ratio * (1 + _SEGMENT_SPREAD)),
        )
        for name, ratio in plan
    ]


def count_chars(text: str) -> int:
    """口播字数：含标点、不含换行与空白。

    服务端实测值。提示词里仍要求模型回填 char_count（让它对预算有自觉），
    但落库一律用这个函数重算 —— 真机上模型把 92 字的文案回填成 135，
    采信它，页面上「字数与时长配不配」的核对就成了误导。
    """
    return sum(1 for ch in text if not ch.isspace())


#: 内置 system 提示词。AI_SYSTEM_PROMPT 配置可整体替换它（留给会写提示词的人）。
#: 注意：DeepSeek 开 response_format=json_object 时要求提示词里出现「JSON」——
#: 下面第 11 条里那个字面量别删。
#:
#: 这套措辞是踩过坑改出来的，改动前请先读这段为什么：
#: - 旧版同时要求「字数落在预算内（一百多字）」与「分 1-3 行、每行不超过 15 字」，
#:   3×15=45 字封顶 vs 一百多字下限，两个要求打架 —— 模型两头占不住，真机上五条
#:   文案全写成了 78–88 字，字数预算被丢掉了。现在行长只作建议、行数放开成不限，
#:   并把「总字数」明确成唯一硬指标。
#: - 模型会把区间当上限用（给 189–232 就写 180 出头），所以 user 消息里另给一个
#:   目标数（见 build_copy_messages）。
#: - 直接点名最常见的失败模式（写七八十字就收尾）比抽象要求有效得多。
_DEFAULT_SYSTEM_PROMPT = """你是一位短视频带货口播稿撰稿人。用户会给你一段视频原有的字幕素材、视频时长和字数预算，你要把它改写成若干条**可以直接拿去配音、并且念满整个视频时长**的广告口播稿。

【最重要的规则：字数】
1. 每条文案的总字数（含标点、不含换行）必须落在给定的字数预算区间内 —— 这是唯一的硬指标，其它要求都要给它让路。
2. 每条写完自己数一遍：不够就补场景、补论据、补具体细节；超了就删形容词、删重复的话。数够了再写下一条。
3. 最常见的错误是「写七八十字就收尾」：那等于视频后半段没有声音，是废稿。宁可啰嗦，不可早收尾。
4. 标点也算字数，不要靠堆标点凑数。
5. 素材只是原料：它可能比你该写的长（挑重点改写），也可能比它短（补充场景和细节，但**不要编造**具体价格、资质、销量、疗效这类事实）。

【怎么分行】
6. 换行只是语气停顿：一句话说完、要喘口气的地方就换行。**行数不限**，两百来字的稿子分成十几行是正常的，不要为了少分行把长句子堆在一起。
7. 每行 12-25 字念起来最顺，但这只是排版建议 —— 任何情况下都不许为了凑行数或压行数而牺牲第 1 条的总字数。

【口播稿的写法】
8. 用口语：短句、常用词，念出来要顺。不要书面语（因此、综上所述、值得注意的是），不要括号、emoji、表情符号。
9. 不要出现只能看不能听的指代（如图、左边这款、下面这个链接）；价格和数量照常写阿拉伯数字即可，配音工具会念出来。
10. 每条走一个不同的切入角度（痛点开场、效果对比、价格锚点、场景代入、行动号召…），几条之间不能只是换几个词。

【输出格式】
11. 只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块。JSON 结构如下（字段名必须逐字一致）：
{
  "analysis": {
    "topic": "素材主题（一句话）",
    "audience": "目标受众（一句话）",
    "selling_points": ["核心卖点1", "核心卖点2"],
    "tone": "调性（如：紧迫促销 / 闺蜜安利 / 专业测评）"
  },
  "copies": [
    {
      "text": "口播稿正文（用 \\n 分行）",
      "angle": "切入角度（4-8 字）",
      "char_count": 210,
      "why": "为什么这么写：结合素材拆解说明这条文案的策略（2-3 句）",
      "highlights": ["好在哪里：要点1", "要点2"],
      "breakdown": [
        {"part": "段落名（与用户给的分段骨架一致）", "content": "这一段在正文里的原文", "explain": "这一段的作用"}
      ]
    }
  ]
}
12. breakdown 必须覆盖整条正文：各段 content 按顺序拼起来（去掉换行）= 正文全文，各段字数之和 = 这条文案的总字数。它同时也是你自查字数的手段。"""


def build_copy_messages(
    material: str,
    duration_seconds: float,
    hint: str,
    count: int,
    budget: Tuple[int, int],
) -> List[Dict[str, str]]:
    """构造 chat/completions 的 messages（system + user）。

    user 消息把五件事说死：视频时长、字数预算（区间 + 一个目标数）、分段骨架、
    要几条、素材正文。

    **为什么要给一个目标数**：模型会把区间当成上限用（给 189–232 就写 180
    出头，真机上写成了 87 字）。给一个数它才会贴着写；区间同时给出，是让它
    知道可以上下浮动多少。目标数取区间中点，必然落在预算内（下限被抬到
    _MIN_BUDGET_LOW 的小视频也一样）。

    **为什么要给分段骨架**：见 _segment_plan —— 让模型只管每段写够，总数
    自然落进预算。各段区间之和 = 目标 ±5%，留给模型的腾挪空间仍在预算内。

    素材字数只作为背景给出（素材就是这段视频原本的口播，长度通常和目标
    接近，是个天然锚点）；长素材不许照抄、短素材不许编造，写在提示词第 5 条里。
    char_count 不信模型（它数不准），解析侧用 count_chars 实测覆盖。
    """
    system = settings.AI_SYSTEM_PROMPT.strip() or _DEFAULT_SYSTEM_PROMPT
    low, high = budget
    target = round((low + high) / 2)

    user_parts = [
        f"视频时长：{duration_seconds:.1f} 秒",
        f"字数预算：每条文案 {low}–{high} 字（含标点、不含换行），按 {target} 字左右来写",
        "分段骨架（各段字数之和 = 这条文案的总字数）：",
    ]
    for name, seg_low, seg_high in _segment_plan(target):
        user_parts.append(f"  · {name}：约 {seg_low}–{seg_high} 字")
    user_parts.append(f"生成条数：{count} 条候选文案")
    if hint:
        user_parts.append(f"补充要求：{hint}")
    user_parts.append(
        f"\n字幕素材（共约 {count_chars(material)} 字，是这段视频原本的口播内容）：\n{material}"
    )
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
