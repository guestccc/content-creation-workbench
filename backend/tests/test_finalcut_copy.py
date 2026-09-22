"""一键成品·文案生成测试：字幕拆解、字数预算、提示词构造、产物解析与执行层全流程。

纯函数（finalcut_copy.py）与 runner（finalcut_copy_runner.py）都在这里；
接口层契约在 test_finalcut_jobs_api.py。runner 测试通过注入 chat_fn 在主线程
同步跑完整个流程，不起线程、不调真 AI。
"""

import json
from pathlib import Path

import pytest

from app.models.finalcut_job import (
    FinalcutCopyJob,
    FinalcutCopyJobStatus,
    FinalcutCopyPhase,
)
from app.services.ai_client import AiError, ChatResult
from app.core.config import settings
from app.services.finalcut_copy import (
    _segment_plan,
    build_copy_messages,
    char_budget,
    parse_copy_payload,
    srt_to_material,
)
from app.services.finalcut_copy_runner import (
    FinalcutCopyRunner,
    claim_next_pending_id,
    recover_interrupted_copy_jobs,
)
from tests.conftest import TestingSessionLocal


@pytest.fixture()
def cps(monkeypatch):
    """把全局语速固定成 5.0 字/秒。

    预算公式吃的是**任务上的语速快照**（页面上每条任务可改），全局值只在
    任务快照是 0.0（升级前的老任务）时兜底 —— 本夹具钉住的就是那条回退路径
    的期望值。不钉的话开发机 `.env` 里恰好有值，断言就随环境飘。
    """
    monkeypatch.setattr(settings, "FINALCUT_CHARS_PER_SECOND", 5.0)
    return 5.0


def _payload(copies: list, **analysis_overrides) -> str:
    """构造一份契约形状的 AI 返回原文。"""
    analysis = {
        "topic": "厨房收纳",
        "audience": "租房年轻人",
        "selling_points": ["免打孔", "承重强"],
        "tone": "闺蜜安利",
    }
    analysis.update(analysis_overrides)
    return json.dumps({"analysis": analysis, "copies": copies}, ensure_ascii=False)


def _copy_entry(text: str, **overrides) -> dict:
    entry = {
        "text": text,
        "angle": "痛点开场",
        "char_count": len(text.replace("\n", "")),
        "why": "先抛痛点再给方案，前 3 秒留住人。",
        "highlights": ["口语化", "有对比"],
        "breakdown": [{"part": "开头", "content": text.split("\n")[0], "explain": "钩子"}],
    }
    entry.update(overrides)
    return entry


# ---------------------------------------------------------------------------
# srt_to_material：字幕文件 → 喂给 AI 的纯素材
# ---------------------------------------------------------------------------


class TestSrtToMaterial:
    def test_strips_sequence_and_timestamp_lines(self):
        srt = (
            "1\n00:00:00,000 --> 00:00:02,000\n这个收纳架真的绝了\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\n厨房瞬间大一倍\n"
        )
        material, truncated = srt_to_material(srt)
        assert truncated is False
        assert material == "这个收纳架真的绝了\n厨房瞬间大一倍"

    def test_dot_separated_timestamp_also_stripped(self):
        srt = "1\n00:00:00.000 --> 00:00:02.000\n点号时间轴也要剔除\n"
        material, _ = srt_to_material(srt)
        assert material == "点号时间轴也要剔除"

    def test_merges_consecutive_duplicate_lines(self):
        """双行重叠字幕会让同一句话相邻出现两遍，喂给模型是噪音。"""
        srt = (
            "1\n00:00:00,000 --> 00:00:02,000\n同一句话\n\n"
            "2\n00:00:01,500 --> 00:00:03,000\n同一句话\n\n"
            "3\n00:00:03,000 --> 00:00:05,000\n下一句话\n"
        )
        material, _ = srt_to_material(srt)
        assert material == "同一句话\n下一句话"

    def test_non_consecutive_duplicates_are_kept(self):
        """不相邻的重复是正常口播（呼应/强调），不能误删。"""
        srt = (
            "1\n00:00:00,000 --> 00:00:02,000\n买它\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\n中间一句\n\n"
            "3\n00:00:04,000 --> 00:00:06,000\n买它\n"
        )
        material, _ = srt_to_material(srt)
        assert material == "买它\n中间一句\n买它"

    def test_empty_text_returns_empty(self):
        assert srt_to_material("") == ("", False)
        assert srt_to_material("  \n\n  ") == ("", False)

    def test_structure_only_file_yields_empty(self):
        """只有序号和时间轴的字幕（如被清空过）拆不出素材。"""
        srt = "1\n00:00:00,000 --> 00:00:02,000\n\n2\n00:00:02,000 --> 00:00:04,000\n"
        material, truncated = srt_to_material(srt)
        assert material == ""
        assert truncated is False

    def test_vtt_header_and_note_stripped(self):
        vtt = (
            "WEBVTT\n\n"
            "NOTE 这是注释\n\n"
            "00:00:00.000 --> 00:00:02.000\n第一句\n"
        )
        material, _ = srt_to_material(vtt)
        assert material == "第一句"

    def test_ass_dialogue_unwrapped(self):
        """ASS 的 Dialogue 行剥掉前 9 个逗号的结构字段，只留文本。"""
        ass = (
            "[Script Info]\n"
            "Title: demo\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\an8}厨房太乱了\\N怎么办\n"
            "Comment: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,注释不算素材\n"
        )
        material, _ = srt_to_material(ass)
        assert material == "厨房太乱了 怎么办"

    def test_truncation_marks_overflow(self):
        material, truncated = srt_to_material("一二三四五六七八九十", max_chars=5)
        assert truncated is True
        assert material.startswith("一二三四五")
        assert "已省略" in material

    def test_exactly_at_limit_not_truncated(self):
        text = "一" * 100
        material, truncated = srt_to_material(text, max_chars=100)
        assert truncated is False
        assert material == text


# ---------------------------------------------------------------------------
# char_budget / _segment_plan：视频时长 → 口播稿字数预算
# ---------------------------------------------------------------------------


class TestCharBudget:
    """语速是**入参**（任务快照），所以这里一律显式传值，不碰 settings。

    runner 侧「任务是 0.0 时回退全局」的那条路径在 TestCopyRunner 里单独钉。
    """

    def test_fifteen_seconds_matches_documented_example(self):
        """15 秒 × 5.0 字/秒 = 75 字 → ±10% 得 68–82（函数 docstring 里钉的样例）。"""
        assert char_budget(15.0, 5.0) == (68, 82)

    def test_tolerance_is_ten_percent(self):
        """用户真实场景钉成回归：36.294 秒的视频、实测语速 5.8 字/秒 → 189–232。"""
        assert char_budget(36.294, 5.8) == (189, 232)

    def test_one_minute(self):
        """60 秒 × 5.0 = 300 字 → 270–330（银行家舍入：round(82.5)=82、round(67.5)=68）。"""
        assert char_budget(60.0, 5.0) == (270, 330)

    def test_slow_rate_short_video_hits_both_floors(self):
        """1.0 字/秒 + 2 秒 = 2 字：下限抬到 10、跨度至少 5，两个保底都要生效。"""
        assert char_budget(2.0, 1.0) == (10, 15)

    def test_zero_duration_has_floor(self):
        """时长为 0 也要有下限兜底：预算不能是 (0, 0)。"""
        assert char_budget(0.0, 5.0) == (10, 15)

    def test_negative_duration_treated_as_zero(self):
        assert char_budget(-3.0, 5.0) == char_budget(0.0, 5.0)

    def test_budget_grows_with_duration(self):
        assert char_budget(60.0, 5.0)[1] > char_budget(15.0, 5.0)[1]
        assert char_budget(60.0, 5.0)[0] > char_budget(15.0, 5.0)[0]

    def test_budget_tracks_rate(self):
        """语速是每条任务各自的值（同一个视频，念快就得多写字）。"""
        fast = char_budget(36.294, 8.0)
        slow = char_budget(36.294, 5.8)
        assert fast[0] > slow[0]

    def test_budget_ignores_global_setting(self, monkeypatch):
        """全局配置不再是预算的来源 —— 它只是新任务的默认值。

        改它不该影响已按任务语速算出来的预算，否则历史任务的展示会漂。
        """
        monkeypatch.setattr(settings, "FINALCUT_CHARS_PER_SECOND", 12.0)
        assert char_budget(36.294, 5.8) == (189, 232)


class TestSegmentPlan:
    def test_four_segments_for_normal_target(self):
        plan = _segment_plan(210)
        assert [name for name, _, _ in plan] == [
            "开头钩子（一句话抓住人）",
            "痛点或场景（说中观众自己的处境）",
            "卖点与证据（凭什么值得买，2-3 个具体的点）",
            "价格与行动号召（怎么买、为什么现在买）",
        ]

    def test_segment_sums_fall_inside_budget(self):
        """把「控总量」降成「控四小段」的前提：分段之和必须仍在整体预算里。

        否则模型照着每段写足了，总数反而冲出预算 —— 那比不给骨架更糟。
        """
        low, high = char_budget(36.294, 5.8)  # (189, 232)
        target = round((low + high) / 2)  # 210
        plan = _segment_plan(target)
        assert sum(seg_low for _, seg_low, _ in plan) >= low
        assert sum(seg_high for _, _, seg_high in plan) <= high

    def test_short_copy_uses_three_segments(self):
        """目标 75 字时四段每段只剩十几字，模型写不出东西 —— 改三段。"""
        plan = _segment_plan(75)
        assert len(plan) == 3
        names = [name for name, _, _ in plan]
        assert "开头钩子（一句话抓住人）" in names
        assert not any("痛点" in name for name in names)

    def test_short_target_boundary_is_inclusive_of_four_segments(self):
        """正好 120 字仍走四段骨架（阈值是「低于」才切）。"""
        assert len(_segment_plan(120)) == 4
        assert len(_segment_plan(119)) == 3


# ---------------------------------------------------------------------------
# build_copy_messages：提示词构造
# ---------------------------------------------------------------------------


class TestBuildCopyMessages:
    def test_user_message_carries_duration_budget_count_and_material(self):
        messages = build_copy_messages("素材正文", 15.0, "", 5, (68, 82))
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        user = messages[1]["content"]
        assert "15.0 秒" in user
        assert "68–82 字" in user
        assert "5 条" in user
        assert "素材正文" in user

    def test_user_message_gives_a_single_target_number(self):
        """模型把区间当上限用（给 189–232 就写 180 出头），必须另给一个目标数。

        目标 = 区间中点：必然落在预算内，模型才有贴着写的锚点。
        """
        user = build_copy_messages("素材", 36.294, "", 5, (189, 232))[1]["content"]
        assert "按 210 字左右来写" in user  # round((189+232)/2) = 210（银行家舍入）
        assert "189–232 字" in user  # 区间仍要给：告诉它能上下浮动多少

    def test_user_message_carries_segment_plan(self):
        """分段骨架是「控总量」的手段，缺了它就只剩一句空泛的总字数要求。"""
        user = build_copy_messages("素材", 15.0, "", 5, (68, 82))[1]["content"]
        assert "分段骨架" in user
        # 目标 75 字走三段骨架（< 120）
        assert "开头钩子（一句话抓住人）：约 21–24 字" in user
        assert "卖点与证据（凭什么值得买）：约 32–35 字" in user
        assert "价格与行动号召（怎么买）：约 18–20 字" in user

    def test_user_message_carries_material_char_count(self):
        """素材字数是天然锚点（素材就是这段视频原本的口播），要报给模型。"""
        user = build_copy_messages("一二三四五", 15.0, "", 5, (68, 82))[1]["content"]
        assert "共约 5 字" in user

    def test_target_is_midpoint_of_any_budget(self):
        """目标数与区间是同时给出的，不是二选一。"""
        user = build_copy_messages("素材", 60.0, "", 5, (270, 330))[1]["content"]
        assert "按 300 字左右来写" in user
        assert "270–330 字" in user

    def test_default_system_prompt_mentions_json(self):
        """DeepSeek 开 response_format=json_object 时要求提示词里出现「JSON」。"""
        messages = build_copy_messages("素材", 15.0, "", 5, (68, 82))
        assert "JSON" in messages[0]["content"]

    def test_default_system_prompt_treats_char_count_as_hard_rule(self):
        """旧提示词「1-3 行、每行 ≤15 字」与字数预算打架（3×15=45 上限 vs 一百多字
        下限），模型两头占不住、五条全写成 87 字。行长必须明确让位于总字数。"""
        system = build_copy_messages("素材", 15.0, "", 5, (68, 82))[0]["content"]
        assert "行数不限" in system
        assert "唯一的硬指标" in system

    def test_hint_included_only_when_present(self):
        with_hint = build_copy_messages("素材", 15.0, "主打性价比", 5, (68, 82))
        assert "补充要求：主打性价比" in with_hint[1]["content"]
        without = build_copy_messages("素材", 15.0, "", 5, (68, 82))
        assert "补充要求" not in without[1]["content"]

    def test_custom_system_prompt_overrides_default(self, monkeypatch):
        monkeypatch.setattr(settings, "AI_SYSTEM_PROMPT", "自定义提示词")
        messages = build_copy_messages("素材", 15.0, "", 5, (68, 82))
        assert messages[0]["content"] == "自定义提示词"


# ---------------------------------------------------------------------------
# parse_copy_payload：AI 返回原文 → (analysis, copies)
# ---------------------------------------------------------------------------


class TestParseCopyPayload:
    def test_plain_json(self):
        raw = _payload([_copy_entry("厨房瞬间\n大一倍")])
        analysis, copies = parse_copy_payload(raw)
        assert analysis["topic"] == "厨房收纳"
        assert len(copies) == 1
        assert copies[0]["text"] == "厨房瞬间\n大一倍"
        assert copies[0]["breakdown"][0]["part"] == "开头"

    def test_fenced_json(self):
        raw = "```json\n" + _payload([_copy_entry("围栏里的文案")]) + "\n```"
        _, copies = parse_copy_payload(raw)
        assert copies[0]["text"] == "围栏里的文案"

    def test_char_count_recomputed_not_trusted(self):
        """模型回填的 char_count 数不准（真机上 92 字的文案回填 135），
        落库必须是服务端实测：含标点、不含换行与空白。"""
        raw = _payload([_copy_entry("两行文案\n第二行！", char_count=999)])
        _, copies = parse_copy_payload(raw)
        # 「两行文案第二行！」= 8 字（换行不计）
        assert copies[0]["char_count"] == 8

    def test_json_embedded_in_prose(self):
        raw = "好的，以下是文案：\n" + _payload([_copy_entry("混在解说里的文案")]) + "\n以上。"
        _, copies = parse_copy_payload(raw)
        assert copies[0]["text"] == "混在解说里的文案"

    def test_analysis_defaults_when_missing(self):
        raw = json.dumps({"copies": [_copy_entry("只有 copies")]}, ensure_ascii=False)
        analysis, copies = parse_copy_payload(raw)
        assert analysis["topic"] == ""
        assert len(copies) == 1

    def test_entries_missing_text_dropped(self):
        raw = _payload([
            _copy_entry(""),
            {"angle": "连 text 键都没有"},
            _copy_entry("有效文案"),
        ])
        _, copies = parse_copy_payload(raw)
        assert [c["text"] for c in copies] == ["有效文案"]

    def test_malformed_entry_dropped_valid_kept(self):
        raw = _payload([
            "不是对象",
            _copy_entry("有效文案", breakdown=["不是字典", {"part": "段", "content": "文", "explain": "释"}]),
        ])
        _, copies = parse_copy_payload(raw)
        assert len(copies) == 1
        assert len(copies[0]["breakdown"]) == 1  # 非字典的 breakdown 段也被丢

    def test_zero_valid_copies_raises(self):
        with pytest.raises(ValueError, match="没有一条可用的文案"):
            parse_copy_payload(_payload([_copy_entry("   ")]))

    def test_empty_copies_list_raises(self):
        with pytest.raises(ValueError):
            parse_copy_payload(_payload([]))

    def test_max_copies_cap(self):
        raw = _payload([_copy_entry(f"第{i}条") for i in range(5)])
        _, copies = parse_copy_payload(raw, max_copies=2)
        assert len(copies) == 2

    def test_bad_json_raises_ai_error(self):
        with pytest.raises(AiError) as exc_info:
            parse_copy_payload("这根本不是 JSON")
        assert exc_info.value.kind == "bad_response"

    def test_top_level_array_raises_ai_error(self):
        with pytest.raises(AiError) as exc_info:
            parse_copy_payload('[{"text": "数组不是契约形状"}]')
        assert exc_info.value.kind == "bad_response"

    def test_legacy_payload_with_target_seconds_still_parses(self):
        """老任务的 result 里残留着 target_seconds（已删字段）。

        模型过去会在 JSON 里回填它、并落进历史任务的 result；CopyCandidate 没开
        extra='forbid'，pydantic v2 默认忽略未知键 —— 历史任务照常打开，
        不需要数据库迁移。回归护栏，不是「顺手保留一个字段」。
        """
        entry = _copy_entry("老文案")
        entry["target_seconds"] = 36.3
        _, copies = parse_copy_payload(_payload([entry]))
        assert copies[0]["text"] == "老文案"
        assert "target_seconds" not in copies[0]


# ---------------------------------------------------------------------------
# runner 全流程（注入 chat_fn，主线程同步跑）
# ---------------------------------------------------------------------------


def _write_srt(tmp_path: Path, content: str = "1\n00:00:00,000 --> 00:00:02,000\n收纳神器\n") -> Path:
    srt = tmp_path / "字幕.srt"
    srt.write_text(content, encoding="utf-8")
    return srt


def _insert_job(
    tmp_path: Path,
    *,
    srt_content: str = "1\n00:00:00,000 --> 00:00:02,000\n收纳神器\n",
    status: str = FinalcutCopyJobStatus.PENDING,
    **overrides,
) -> int:
    """直接落一条任务记录（绕过 service 的创建校验），返回任务 id。"""
    srt = _write_srt(tmp_path, srt_content)
    fields = {
        "status": status,
        "subtitle_path": str(srt),
        "video_path": str(tmp_path / "成片.mp4"),
        "video_duration": 15.0,
        "copy_count": 3,
        "hint": "",
        "model": "deepseek-chat",
    }
    fields.update(overrides)
    with TestingSessionLocal() as db:
        job = FinalcutCopyJob(**fields)
        db.add(job)
        db.commit()
        return int(job.id)


def _fetch(job_id: int) -> FinalcutCopyJob:
    with TestingSessionLocal() as db:
        job = db.get(FinalcutCopyJob, job_id)
        assert job is not None
        # expire_on_commit=False 的会话外对象：属性在会话关闭前全部加载出来
        db.expunge(job)
        return job


def _success_chat(messages):
    content = _payload([
        _copy_entry("厨房瞬间\n大一倍"),
        _copy_entry("免打孔收纳\n租房也能用"),
    ])
    return ChatResult(content=content, usage={"total_tokens": 321})


@pytest.mark.usefixtures("db_session")
class TestCopyRunner:
    def test_success_full_flow(self, tmp_path):
        job_id = _insert_job(tmp_path)
        runner = FinalcutCopyRunner(session_factory=TestingSessionLocal, chat_fn=_success_chat)

        assert runner.run_job(job_id) is True

        job = _fetch(job_id)
        assert job.status == FinalcutCopyJobStatus.SUCCESS
        assert job.progress_percent == 100.0
        assert job.current_phase == FinalcutCopyPhase.PARSE
        assert job.tokens_used == 321
        assert job.finished_at is not None
        assert job.result["analysis"]["topic"] == "厨房收纳"
        assert len(job.result["copies"]) == 2
        assert "大一倍" in job.raw_response

    def test_prompt_carries_duration_and_budget(self, tmp_path, cps):
        """runner 必须用**当次生效的**语速算预算，而不是某个写死的常量。

        这条走的正是「老任务」路径：`_insert_job` 不传语速 → 列上是 0.0 →
        回退全局值（cps 钉成 5.0）。这条路不是摆设：升级时停在 pending 的
        老任务 `recover_interrupted_copy_jobs` 不管（它只收拾 running），
        重启后照样被认领执行，带着 0.0 进 runner。
        """
        job_id = _insert_job(tmp_path)
        captured = []

        def spy_chat(messages):
            captured.extend(messages)
            return _success_chat(messages)

        runner = FinalcutCopyRunner(session_factory=TestingSessionLocal, chat_fn=spy_chat)
        runner.run_job(job_id)

        user = captured[1]["content"]
        assert "15.0 秒" in user
        assert "68–82 字" in user  # 15 × 5.0 = 75 字 ±10%
        assert "分段骨架" in user
        assert "收纳神器" in user  # 字幕拆解出的素材进了提示词

    def test_task_rate_wins_over_global(self, tmp_path, cps):
        """任务上的语速快照优先于全局配置 —— 「这条念快些」就靠它。

        cps fixture 把全局钉在 5.0；任务自己带 5.8，预算必须是按 5.8 算的
        （15 × 5.8 = 87 字 ±10% → 78–96），不能被全局值盖掉。
        """
        job_id = _insert_job(tmp_path, chars_per_second=5.8)
        captured = []

        def spy_chat(messages):
            captured.extend(messages)
            return _success_chat(messages)

        runner = FinalcutCopyRunner(session_factory=TestingSessionLocal, chat_fn=spy_chat)
        runner.run_job(job_id)

        user = captured[1]["content"]
        assert "78–96 字" in user
        assert "68–82 字" not in user  # 不是全局 5.0 算出来的

    def test_auth_error_lands_user_message(self, tmp_path):
        """401 的 error_message 要能引导用户去改 key，而不是堆栈。"""
        job_id = _insert_job(tmp_path)

        def auth_fail(messages):
            raise AiError("auth", "AI 接口拒绝了 API key（401/403）：请检查 AI_API_KEY")

        runner = FinalcutCopyRunner(session_factory=TestingSessionLocal, chat_fn=auth_fail)
        runner.run_job(job_id)

        job = _fetch(job_id)
        assert job.status == FinalcutCopyJobStatus.FAILED
        assert "AI_API_KEY" in job.error_message
        assert job.result is None

    def test_timeout_error(self, tmp_path):
        job_id = _insert_job(tmp_path)

        def timeout_fail(messages):
            raise AiError("timeout", "AI 请求超时（超过 120 秒）")

        runner = FinalcutCopyRunner(session_factory=TestingSessionLocal, chat_fn=timeout_fail)
        runner.run_job(job_id)

        job = _fetch(job_id)
        assert job.status == FinalcutCopyJobStatus.FAILED
        assert "超时" in job.error_message

    def test_unexpected_chat_exception_also_fails(self, tmp_path):
        """注入的 chat_fn 抛出非 AiError 时，任务同样要落成 failed，不能挂着。"""
        job_id = _insert_job(tmp_path)

        def boom(messages):
            raise RuntimeError("预料之外的错误")

        runner = FinalcutCopyRunner(session_factory=TestingSessionLocal, chat_fn=boom)
        runner.run_job(job_id)
        assert _fetch(job_id).status == FinalcutCopyJobStatus.FAILED

    def test_bad_response_keeps_raw(self, tmp_path):
        job_id = _insert_job(tmp_path)

        def garbage(messages):
            return ChatResult(content="抱歉，我没法生成", usage={})

        runner = FinalcutCopyRunner(session_factory=TestingSessionLocal, chat_fn=garbage)
        runner.run_job(job_id)

        job = _fetch(job_id)
        assert job.status == FinalcutCopyJobStatus.FAILED
        assert job.raw_response == "抱歉，我没法生成"  # 排查全靠原文

    def test_zero_valid_copies_fails_with_raw(self, tmp_path):
        job_id = _insert_job(tmp_path)

        def empty_copies(messages):
            return ChatResult(content=_payload([]), usage={})

        runner = FinalcutCopyRunner(session_factory=TestingSessionLocal, chat_fn=empty_copies)
        runner.run_job(job_id)

        job = _fetch(job_id)
        assert job.status == FinalcutCopyJobStatus.FAILED
        assert "没有一条可用的文案" in job.error_message
        assert job.raw_response  # 原文落库

    def test_cancel_at_phase_boundary_discards_result(self, tmp_path):
        """AI 在途时点了取消：请求跑完，但结果不落库，状态保持 cancelled。"""
        job_id = _insert_job(tmp_path)

        def cancelling_chat(messages):
            # 模拟「HTTP 在途时用户点了取消」：另一个会话把状态列置为 cancelled
            with TestingSessionLocal() as db:
                job = db.get(FinalcutCopyJob, job_id)
                job.status = FinalcutCopyJobStatus.CANCELLED
                db.commit()
            return _success_chat(messages)

        runner = FinalcutCopyRunner(session_factory=TestingSessionLocal, chat_fn=cancelling_chat)
        runner.run_job(job_id)

        job = _fetch(job_id)
        assert job.status == FinalcutCopyJobStatus.CANCELLED  # 不被回写成 success
        assert job.result is None

    def test_missing_subtitle_fails(self, tmp_path):
        job_id = _insert_job(tmp_path)
        Path(_fetch(job_id).subtitle_path).unlink()  # 排队期间字幕被删了

        runner = FinalcutCopyRunner(session_factory=TestingSessionLocal, chat_fn=_success_chat)
        runner.run_job(job_id)

        job = _fetch(job_id)
        assert job.status == FinalcutCopyJobStatus.FAILED
        assert "字幕文件读不出来" in job.error_message

    def test_empty_subtitle_fails(self, tmp_path):
        job_id = _insert_job(tmp_path, srt_content="1\n00:00:00,000 --> 00:00:02,000\n")

        runner = FinalcutCopyRunner(session_factory=TestingSessionLocal, chat_fn=_success_chat)
        runner.run_job(job_id)

        job = _fetch(job_id)
        assert job.status == FinalcutCopyJobStatus.FAILED
        assert "没有可用文本" in job.error_message

    def test_run_job_only_claims_pending(self, tmp_path):
        """终态/运行中的任务不能再被认领执行。"""
        job_id = _insert_job(tmp_path, status=FinalcutCopyJobStatus.SUCCESS)
        runner = FinalcutCopyRunner(session_factory=TestingSessionLocal, chat_fn=_success_chat)
        assert runner.run_job(job_id) is False
        assert _fetch(job_id).status == FinalcutCopyJobStatus.SUCCESS  # 原样不动

    def test_claim_next_pending_id_picks_earliest(self, tmp_path):
        first = _insert_job(tmp_path)
        second = _insert_job(tmp_path, hint="第二个")
        assert claim_next_pending_id(session_factory=TestingSessionLocal) == first
        # 第一个被标成 running 后轮到第二个
        with TestingSessionLocal() as db:
            job = db.get(FinalcutCopyJob, first)
            job.status = FinalcutCopyJobStatus.RUNNING
            db.commit()
        assert claim_next_pending_id(session_factory=TestingSessionLocal) == second

    def test_recover_marks_running_failed(self, tmp_path):
        """服务重启：running 的文案任务标 failed（没有子进程要杀，只说圆状态）。"""
        stuck = _insert_job(tmp_path, status=FinalcutCopyJobStatus.RUNNING)
        pending = _insert_job(tmp_path)

        recovered = recover_interrupted_copy_jobs(session_factory=TestingSessionLocal)

        assert recovered == 1
        job = _fetch(stuck)
        assert job.status == FinalcutCopyJobStatus.FAILED
        assert "服务重启" in job.error_message
        assert _fetch(pending).status == FinalcutCopyJobStatus.PENDING  # pending 不动
