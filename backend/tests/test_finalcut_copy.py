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
from app.services.finalcut_copy import (
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
        "target_seconds": 15,
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
# char_budget：视频时长 → 上屏字数预算
# ---------------------------------------------------------------------------


class TestCharBudget:
    def test_fifteen_seconds_matches_documented_example(self):
        """15s × 4.5 字/秒 ≈ 47–68 字（函数 docstring 里钉的样例）。"""
        assert char_budget(15.0) == (47, 68)

    def test_zero_duration_has_floor(self):
        """时长为 0 也要有下限兜底：预算不能是 (0, 0)。"""
        low, high = char_budget(0.0)
        assert low == 10
        assert high > low

    def test_negative_duration_treated_as_zero(self):
        assert char_budget(-3.0) == char_budget(0.0)

    def test_budget_grows_with_duration(self):
        assert char_budget(60.0)[1] > char_budget(15.0)[1]
        assert char_budget(60.0)[0] > char_budget(15.0)[0]


# ---------------------------------------------------------------------------
# build_copy_messages：提示词构造
# ---------------------------------------------------------------------------


class TestBuildCopyMessages:
    def test_user_message_carries_duration_budget_count_and_material(self):
        messages = build_copy_messages("素材正文", 15.0, "", 5, (47, 68))
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        user = messages[1]["content"]
        assert "15.0 秒" in user
        assert "47–68 字" in user
        assert "5 条" in user
        assert "素材正文" in user

    def test_default_system_prompt_mentions_json(self):
        """DeepSeek 开 response_format=json_object 时要求提示词里出现「JSON」。"""
        messages = build_copy_messages("素材", 15.0, "", 5, (47, 68))
        assert "JSON" in messages[0]["content"]

    def test_hint_included_only_when_present(self):
        with_hint = build_copy_messages("素材", 15.0, "主打性价比", 5, (47, 68))
        assert "补充要求：主打性价比" in with_hint[1]["content"]
        without = build_copy_messages("素材", 15.0, "", 5, (47, 68))
        assert "补充要求" not in without[1]["content"]

    def test_custom_system_prompt_overrides_default(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "AI_SYSTEM_PROMPT", "自定义提示词")
        messages = build_copy_messages("素材", 15.0, "", 5, (47, 68))
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

    def test_prompt_carries_duration_and_budget(self, tmp_path):
        job_id = _insert_job(tmp_path)
        captured = []

        def spy_chat(messages):
            captured.extend(messages)
            return _success_chat(messages)

        runner = FinalcutCopyRunner(session_factory=TestingSessionLocal, chat_fn=spy_chat)
        runner.run_job(job_id)

        user = captured[1]["content"]
        assert "15.0 秒" in user
        assert "47–68 字" in user
        assert "收纳神器" in user  # 字幕拆解出的素材进了提示词

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
