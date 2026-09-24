"""笔记 AI 文案的测试：提示词/解析纯函数 + 接口层契约（含错误码映射与级联删除）。

AI 调用一律走假函数（monkeypatch 模块里的 stream_chat），测试里不发任何网络请求；
产物用 tmp_path 里的假 jsonl 驱动（conftest._isolate_crawl_output）。

生成接口是 SSE 流（`POST /ai-copies/stream`），断言都建立在拆出来的帧上 ——
见 `_stream_copy`。⚠️ 流里的写库用的是**另一个会话**（会话工厂注入的那条线，
见 app/api/deps.py），所以断言读数据走 db_session 是跨会话的：靠 conftest 的
StaticPool 共用同一个内存库实例。
"""

import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.exceptions import DatabaseError
from app.models.crawl_ai_copy import CrawlNoteAiCopy
from app.services.ai_client import AiError, ChatDelta
from app.services.crawl_note_copy import (
    NOTE_DESC_MAX_CHARS,
    build_copy_messages,
    parse_copy_payload,
)
from app.services.crawl_note_copy_service import CrawlNoteCopyService
from tests.fakes import mc_note

pytestmark = pytest.mark.usefixtures("_isolate_crawl_output")


def _create_job(client) -> dict:
    """建一条默认的 xhs 搜索任务并返回响应 data。"""
    response = client.post(
        "/api/v1/crawl/jobs",
        json={
            "platform": "xhs",
            "crawler_type": "search",
            "login_type": "qrcode",
            "keywords": ["保温杯"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _write_results(job: dict, note_ids=("note1", "note2")) -> None:
    """往任务的输出目录里写一份假 jsonl（模拟 MC 落盘）。"""
    target = Path(job["output_dir"]) / "xhs" / "jsonl"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "search_contents_2026-09-19.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for note_id in note_ids:
            handle.write(json.dumps(mc_note(note_id), ensure_ascii=False) + "\n")


def _payload(titles=("t1", "t2", "t3", "t4", "t5"), intros=("i1", "i2", "i3")) -> str:
    """一份形状正确的模型输出（两个平台同内容）。"""
    return json.dumps(
        {
            "xhs": {"titles": list(titles), "intros": list(intros)},
            "dy": {"titles": list(titles), "intros": list(intros)},
        },
        ensure_ascii=False,
    )


def _fake_chat(content: str, tokens: int = 42, reasoning: str = ""):
    """构造一个假 chat_fn（生成器，按增量吐思维链与正文）。

    思维链与正文都**切成两段**吐：服务层要的是「边到边推 + 自己拼接」，
    只吐一整块的话「拼接」这段逻辑永远得不到检验。
    """

    def _chat(messages):
        if reasoning:
            half = len(reasoning) // 2
            yield ChatDelta(reasoning[:half], "", {})
            yield ChatDelta(reasoning[half:], "", {})
        cut = len(content) // 2
        yield ChatDelta("", content[:cut], {})
        yield ChatDelta("", content[cut:], {})
        # 收尾帧：形状与 DeepSeek 的 include_usage 末帧一致（只有 usage）
        yield ChatDelta("", "", {"total_tokens": tokens})

    return _chat


def _patch_chat(monkeypatch, fake) -> None:
    """把服务层用的 stream_chat 换成假函数（调用时才取，见 service.stream 注释）。"""
    monkeypatch.setattr("app.services.crawl_note_copy_service.stream_chat", fake)


def _parse_sse(text: str) -> list:
    """把 SSE 响应体拆成 [(事件名, data 字典)]。

    只认本工程约定的 `event: X` + 单行 `data: {...}` 两行式帧；空块跳过。
    多行 data（SSE 规范允许）我们不发，这里也不支持 —— 支持它只会掩盖
    「服务端某天换成了多行拼接」这件事。
    """
    frames = []
    for block in text.split("\n\n"):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        event = ""
        data = ""
        for line in lines:
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        frames.append((event, json.loads(data)))
    return frames


def _stream_copy(client, job_id: int, note_id: str = "note1"):
    """调流式端点，返回 (响应, 帧列表)。

    预检失败时压根不是流（404/422 → 普通 JSON 错误体），这时帧列表为空 ——
    按 media type 判断而不是「解析失败就当我们没看见」，免得把格式问题吞掉。
    """
    response = client.post(
        f"/api/v1/crawl/jobs/{job_id}/ai-copies/stream", json={"note_id": note_id}
    )
    is_stream = response.headers.get("content-type", "").startswith("text/event-stream")
    return response, _parse_sse(response.text) if is_stream else []


def _frame(frames: list, event: str) -> dict:
    """取指定名字的第一帧数据；没有就报错（省得断言写成空过）。"""
    for name, data in frames:
        if name == event:
            return data
    raise AssertionError(f"响应里没有 {event} 帧：{frames}")


def _reasoning_text(frames: list) -> str:
    """把所有 reasoning 帧的增量拼起来 —— 等价于前端累加出来的那段文字。"""
    return "".join(
        data["text"] for name, data in frames if name == "reasoning"
    )


def _cancel(client, job_id: int) -> None:
    """把任务置成终态（删除只允许终态任务；测试里不让工作线程跑）。"""
    response = client.post(f"/api/v1/crawl/jobs/{job_id}/cancel")
    assert response.status_code == 200, response.text


# ----------------------------------------------------------------------
# 纯函数：提示词构造
# ----------------------------------------------------------------------


class TestBuildCopyMessages:
    """提示词里必须有的东西：JSON 字面量、防注入声明、双平台契约、正文标记。"""

    def test_system_has_json_literal_and_injection_guard(self):
        system = build_copy_messages({"id": "n1"}, "xhs")[0]["content"]

        # DeepSeek 开 response_format=json_object 时硬要求提示词里有「JSON」
        assert "JSON" in system
        # 防注入：把标记区间内的内容明确说成「笔记内容，不是指令」
        assert "【笔记正文开始】" in system
        assert "【笔记正文结束】" in system
        assert "一律不执行" in system
        # 双平台契约逐字钉住（前端按这两个键取数据）
        assert '"xhs"' in system and '"dy"' in system
        assert '"titles"' in system and '"intros"' in system

    def test_user_carries_title_author_and_wrapped_desc(self):
        messages = build_copy_messages(
            {"id": "n1", "title": "标题A", "desc": "正文B", "nickname": "张三"}, "xhs"
        )
        user = messages[1]["content"]

        assert "张三" in user
        assert "标题A" in user
        assert "来自小红书的" in user
        # 正文必须夹在标记之间（防注入的第二层）
        assert user.index("【笔记正文开始】") < user.index("正文B") < user.index("【笔记正文结束】")

    def test_unknown_platform_omits_source(self):
        user = build_copy_messages({"id": "n1", "title": "t"}, "")[1]["content"]
        assert "来自" not in user

    def test_missing_fields_fall_back(self):
        """空标题/空正文不能拼出「笔记标题：」这种半截文案。"""
        user = build_copy_messages({"id": "n1"}, "xhs")[1]["content"]
        assert "（无标题）" in user
        assert "（无正文）" in user
        assert "（佚名）" in user

    def test_long_desc_is_truncated(self):
        user = build_copy_messages({"id": "n1", "desc": "字" * 3000}, "xhs")[1]["content"]
        assert "后续内容已省略" in user
        # 截断到 NOTE_DESC_MAX_CHARS，外加一行省略标记
        assert user.count("字") == NOTE_DESC_MAX_CHARS


# ----------------------------------------------------------------------
# 纯函数：产物解析
# ----------------------------------------------------------------------


class TestParseCopyPayload:
    """解析的容错边界：围栏、条数、空条目、缺平台、坏 JSON。"""

    def test_standard_shape(self):
        parsed = parse_copy_payload(_payload())
        assert len(parsed["xhs"]["titles"]) == 5
        assert len(parsed["xhs"]["intros"]) == 3
        assert len(parsed["dy"]["titles"]) == 5

    def test_fenced_json(self):
        parsed = parse_copy_payload(f"```json\n{_payload()}\n```")
        assert parsed["xhs"]["titles"] == ["t1", "t2", "t3", "t4", "t5"]

    def test_over_limit_is_truncated(self):
        parsed = parse_copy_payload(
            _payload(titles=("a", "b", "c", "d", "e", "f", "g"), intros=("1", "2", "3", "4", "5"))
        )
        assert parsed["xhs"]["titles"] == ["a", "b", "c", "d", "e"]  # 7 → 5
        assert parsed["xhs"]["intros"] == ["1", "2", "3"]  # 5 → 3

    def test_under_limit_is_kept_as_is(self):
        """少给不补齐：模型给 2 条就 2 条，前端按实际渲染。"""
        parsed = parse_copy_payload(_payload(titles=("a", "b"), intros=()))
        assert parsed["xhs"]["titles"] == ["a", "b"]
        assert parsed["xhs"]["intros"] == []

    def test_blank_and_non_string_items_are_dropped(self):
        parsed = parse_copy_payload(
            json.dumps(
                {
                    "xhs": {"titles": ["  ", "真的", None, 123], "intros": ["", " 简介 "]},
                    "dy": {"titles": ["d"], "intros": []},
                },
                ensure_ascii=False,
            )
        )
        assert parsed["xhs"]["titles"] == ["真的", "123"]
        assert parsed["xhs"]["intros"] == ["简介"]

    def test_missing_platform_raises(self):
        """缺 dy 键 = 抖音一条都没有：两个平台要一起发，只出一半算失败。"""
        with pytest.raises(ValueError):
            parse_copy_payload(json.dumps({"xhs": {"titles": ["a"], "intros": []}}))

    def test_empty_titles_raises(self):
        with pytest.raises(ValueError):
            parse_copy_payload(
                json.dumps(
                    {"xhs": {"titles": ["a"], "intros": []}, "dy": {"titles": [], "intros": ["b"]}}
                )
            )

    def test_titles_only_in_intros_raises(self):
        """简介有、标题没有同样算失败（标题是发布时的必填项）。"""
        with pytest.raises(ValueError):
            parse_copy_payload(
                json.dumps(
                    {"xhs": {"titles": [], "intros": ["i"]}, "dy": {"titles": [], "intros": ["i"]}}
                )
            )

    def test_non_dict_section_is_empty(self):
        with pytest.raises(ValueError):
            parse_copy_payload(json.dumps({"xhs": ["不是对象"], "dy": {"titles": ["d"]}}))

    def test_top_level_array_reports_generic_message(self):
        """顶层是数组 → AiError(bad_response)，且文案不能透出 finalcut 的契约名。"""
        with pytest.raises(AiError) as caught:
            parse_copy_payload("[]")
        assert caught.value.kind == "bad_response"
        assert "analysis, copies" not in caught.value.user_message
        assert "换一批" in caught.value.user_message

    def test_garbage_reports_generic_message(self):
        with pytest.raises(AiError) as caught:
            parse_copy_payload("模型今天不想干活")
        assert caught.value.kind == "bad_response"
        assert "analysis, copies" not in caught.value.user_message


# ----------------------------------------------------------------------
# 接口层
# ----------------------------------------------------------------------


class TestNoteAiCopyStream:
    """流式生成端点的帧契约。"""

    def test_reasoning_frames_then_done(self, client, monkeypatch):
        """思维链逐块到达、done 收尾；帧里带上落库结果。"""
        job = _create_job(client)
        _write_results(job)
        _patch_chat(monkeypatch, _fake_chat(_payload(), tokens=1234, reasoning="先看标题再看正文"))

        response, frames = _stream_copy(client, job["id"])
        assert response.status_code == 200, response.text
        # 前端按 media type 判流；顺便钉住别被 FastAPI 默认的 application/json 顶掉
        assert response.headers["content-type"].startswith("text/event-stream")

        assert [name for name, _ in frames] == [
            "reasoning", "reasoning", "done",
        ]
        assert _reasoning_text(frames) == "先看标题再看正文"
        # 中间那两块正文增量不该以 reasoning 帧的形式漏出来
        assert "t1" not in _reasoning_text(frames)

        data = _frame(frames, "done")["result"]
        assert data["job_id"] == job["id"]
        assert data["note_id"] == "note1"
        assert data["platforms"]["xhs"]["titles"] == ["t1", "t2", "t3", "t4", "t5"]
        assert data["platforms"]["dy"]["intros"] == ["i1", "i2", "i3"]
        assert data["tokens_used"] == 1234
        assert data["model"] == settings.AI_MODEL
        assert data["reasoning"] == "先看标题再看正文"

    def test_thinking_is_persisted_for_later_review(self, client, monkeypatch):
        """回看场景：思维链落库，GET 时原样带回来。"""
        job = _create_job(client)
        _write_results(job)
        _patch_chat(monkeypatch, _fake_chat(_payload(), reasoning="第一段思考第二段思考"))

        _stream_copy(client, job["id"])

        fetched = client.get(
            f"/api/v1/crawl/jobs/{job['id']}/ai-copies", params={"note_id": "note1"}
        ).json()["data"]
        assert fetched["found"] is True
        assert fetched["result"]["reasoning"] == "第一段思考第二段思考"
        assert fetched["result"]["platforms"]["xhs"]["titles"][0] == "t1"

    def test_get_before_generate_reports_not_found(self, client):
        job = _create_job(client)
        _write_results(job)

        response = client.get(
            f"/api/v1/crawl/jobs/{job['id']}/ai-copies", params={"note_id": "note1"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"found": False, "result": None}

    def test_model_without_thinking_still_succeeds(self, client, monkeypatch):
        """模型这轮没吐思维链（或 AI_THINKING=false）：一帧 reasoning 都没有，
        但生成照常成功 —— 前端据此整块不渲染，不是错误。"""
        job = _create_job(client)
        _write_results(job)
        _patch_chat(monkeypatch, _fake_chat(_payload(), reasoning=""))

        _, frames = _stream_copy(client, job["id"])
        assert [name for name, _ in frames] == ["done"]
        assert _frame(frames, "done")["result"]["reasoning"] == ""

    def test_regenerate_overwrites_single_row(self, client, db_session, monkeypatch):
        """「换一批」是覆盖语义：同一（任务, 笔记）永远只有一行，思维链跟着换。"""
        job = _create_job(client)

        _write_results(job)
        _patch_chat(
            monkeypatch, _fake_chat(_payload(titles=("旧1", "旧2")), reasoning="旧的思考")
        )
        _, first_frames = _stream_copy(client, job["id"])
        first = _frame(first_frames, "done")["result"]

        _patch_chat(
            monkeypatch, _fake_chat(_payload(titles=("新1", "新2")), reasoning="新的思考")
        )
        _, second_frames = _stream_copy(client, job["id"])
        second = _frame(second_frames, "done")["result"]

        assert second["platforms"]["xhs"]["titles"] == ["新1", "新2"]
        rows = db_session.query(CrawlNoteAiCopy).all()
        assert len(rows) == 1
        assert rows[0].payload["xhs"]["titles"] == ["新1", "新2"]
        assert rows[0].reasoning == "新的思考"
        assert second["updated_at"] >= first["updated_at"]

    def test_two_notes_keep_two_rows(self, client, db_session, monkeypatch):
        job = _create_job(client)
        _write_results(job)
        _patch_chat(monkeypatch, _fake_chat(_payload()))

        for note_id in ("note1", "note2"):
            response, frames = _stream_copy(client, job["id"], note_id)
            assert response.status_code == 200, response.text
            assert "result" in _frame(frames, "done")

        assert db_session.query(CrawlNoteAiCopy).count() == 2

    # ------------------------------------------------------------------
    # 预检失败：还没开流，回的是普通 JSON 错误（不是 error 帧）
    # ------------------------------------------------------------------

    def test_unknown_job_returns_404(self, client):
        response, frames = _stream_copy(client, 9999)
        assert response.status_code == 404
        assert frames == []

    def test_note_not_in_results_returns_404(self, client, monkeypatch):
        job = _create_job(client)
        _write_results(job, note_ids=("note1",))
        _patch_chat(monkeypatch, _fake_chat(_payload()))

        response, frames = _stream_copy(client, job["id"], "不存在")
        assert response.status_code == 404
        assert "不在任务结果里" in response.json()["error"]["message"]
        assert frames == []

    def test_blank_note_id_returns_422(self, client):
        job = _create_job(client)
        response = client.post(
            f"/api/v1/crawl/jobs/{job['id']}/ai-copies/stream", json={"note_id": ""}
        )
        assert response.status_code == 422

    # ------------------------------------------------------------------
    # 生成期失败：流已经开了，只能走 error 帧
    # ------------------------------------------------------------------

    def test_unconfigured_ai_reports_config_code(self, client, monkeypatch):
        """未配置 key：给 AI_NOT_CONFIGURED，前端据此弹「去配置」。"""
        job = _create_job(client)
        _write_results(job)
        monkeypatch.setattr(settings, "AI_API_KEY", "")

        response, frames = _stream_copy(client, job["id"])
        assert response.status_code == 200, response.text
        assert _frame(frames, "error")["code"] == "AI_NOT_CONFIGURED"
        assert frames[-1][0] == "error"

    @pytest.mark.parametrize(
        ("kind", "code"),
        [
            ("auth", "AI_AUTH_FAILED"),
            ("rate_limit", "AI_RATE_LIMIT"),
            ("timeout", "AI_TIMEOUT"),
            ("bad_response", "AI_BAD_RESPONSE"),
            ("network", "AI_UPSTREAM"),
            ("http", "AI_UPSTREAM"),
            ("unknown", "AI_UPSTREAM"),
        ],
    )
    def test_ai_error_kinds_map_to_codes(self, client, monkeypatch, kind, code):
        job = _create_job(client)
        _write_results(job)

        def boom(messages, _kind=kind):
            raise AiError(_kind, f"假失败：{_kind}")

        _patch_chat(monkeypatch, boom)
        _, frames = _stream_copy(client, job["id"])
        assert [name for name, _ in frames] == ["error"]
        assert _frame(frames, "error")["code"] == code
        assert f"假失败：{kind}" in _frame(frames, "error")["message"]

    def test_unexpected_exception_becomes_upstream_error(self, client, monkeypatch):
        """假函数抛的不是 AiError（真跑时可能是任何东西）：也得变成 error 帧，
        不能让流就那么断掉 —— 断了前端只看到「网络错误」，什么提示都没有。"""
        job = _create_job(client)
        _write_results(job)

        def boom(messages):
            raise RuntimeError("数据库连接没了")

        _patch_chat(monkeypatch, boom)
        _, frames = _stream_copy(client, job["id"])
        assert [name for name, _ in frames] == ["error"]
        assert _frame(frames, "error")["code"] == "AI_UPSTREAM"
        assert "数据库连接没了" in _frame(frames, "error")["message"]

    def test_half_reasoning_survives_a_late_failure(self, client, monkeypatch):
        """思维链吐到一半才失败：已经推出去的那截收不回来，但错误要说清楚。"""
        job = _create_job(client)
        _write_results(job)

        def boom(messages):
            yield ChatDelta("想了半截", "", {})
            raise AiError("timeout", "AI 请求超时")

        _patch_chat(monkeypatch, boom)
        _, frames = _stream_copy(client, job["id"])
        assert [name for name, _ in frames] == ["reasoning", "error"]
        assert _reasoning_text(frames) == "想了半截"
        assert _frame(frames, "error")["code"] == "AI_TIMEOUT"

    def test_unparsable_payload_stores_nothing(self, client, db_session, monkeypatch):
        job = _create_job(client)
        _write_results(job)
        _patch_chat(monkeypatch, _fake_chat("模型今天不想干活"))

        _, frames = _stream_copy(client, job["id"])
        assert _frame(frames, "error")["code"] == "AI_BAD_RESPONSE"
        assert frames[-1][0] == "error"
        # 失败不落行：库里有内容 = 有一份能用的文案
        assert db_session.query(CrawlNoteAiCopy).count() == 0

    def test_db_failure_reports_db_error(self, client, db_session, monkeypatch):
        """写库失败：事务已回滚，但流已经开了 —— 只能给 error 帧（码是 DB_ERROR）。

        AI 那一半是成功的，所以这里必须把 _persist 打桩打掉，而不是伪造 chat。"""
        job = _create_job(client)
        _write_results(job)
        _patch_chat(monkeypatch, _fake_chat(_payload()))

        def boom(self, db, **kwargs):
            raise DatabaseError("保存笔记 AI 文案失败")

        monkeypatch.setattr(CrawlNoteCopyService, "_persist", boom)
        _, frames = _stream_copy(client, job["id"])
        assert _frame(frames, "error")["code"] == "DB_ERROR"
        assert db_session.query(CrawlNoteAiCopy).count() == 0

    def test_missing_platform_reports_bad_response(self, client, db_session, monkeypatch):
        job = _create_job(client)
        _write_results(job)
        _patch_chat(
            monkeypatch,
            _fake_chat(json.dumps({"xhs": {"titles": ["a"], "intros": []}})),
        )

        _, frames = _stream_copy(client, job["id"])
        assert _frame(frames, "error")["code"] == "AI_BAD_RESPONSE"
        assert db_session.query(CrawlNoteAiCopy).count() == 0

    # ------------------------------------------------------------------
    # 级联删除
    # ------------------------------------------------------------------

    def test_deleting_job_cascades_to_ai_copies(self, client, db_session, monkeypatch):
        """删任务连带删文案；别的任务的文案不能跟着消失。"""
        keep = _create_job(client)
        drop = _create_job(client)
        _write_results(keep, note_ids=("note1",))
        _write_results(drop, note_ids=("note1",))
        _patch_chat(monkeypatch, _fake_chat(_payload()))

        for job in (keep, drop):
            _, frames = _stream_copy(client, job["id"])
            assert "result" in _frame(frames, "done")
        assert db_session.query(CrawlNoteAiCopy).count() == 2

        _cancel(client, drop["id"])
        deleted = client.delete(f"/api/v1/crawl/jobs/{drop['id']}")
        assert deleted.status_code == 200, deleted.text

        remaining = db_session.query(CrawlNoteAiCopy).all()
        assert [row.crawl_job_id for row in remaining] == [keep["id"]]

    def test_batch_delete_cascades_to_ai_copies(self, client, db_session, monkeypatch):
        keep = _create_job(client)
        drop_a = _create_job(client)
        drop_b = _create_job(client)
        for job in (keep, drop_a, drop_b):
            _write_results(job, note_ids=("note1",))
        _patch_chat(monkeypatch, _fake_chat(_payload()))

        for job in (keep, drop_a, drop_b):
            _stream_copy(client, job["id"])
        assert db_session.query(CrawlNoteAiCopy).count() == 3

        for job in (drop_a, drop_b):
            _cancel(client, job["id"])
        response = client.post(
            "/api/v1/crawl/jobs/batch-delete",
            json={"ids": [drop_a["id"], drop_b["id"]]},
        )
        assert response.status_code == 200, response.text

        remaining = db_session.query(CrawlNoteAiCopy).all()
        assert [row.crawl_job_id for row in remaining] == [keep["id"]]
