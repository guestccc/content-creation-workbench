"""
vctl/ui.py 的单元测试。

关注点是**纯逻辑与分支**，不驱动真实终端、不依赖 questionary 真的装好 ——
questionary 用 _FakeQuestionary 顶替，这样在管道、CI、无 TTY 的环境里也能跑。

重点覆盖三处最容易出事的地方：
1. rich_enabled() 的四个降级条件（任一不满足都必须回退到文本输入）
2. _safe_default() —— questionary 在**构造 select 时**就校验 default，
   传了不在候选项里的值会直接 ValueError（配置文件里的过期值常年触发这个）
3. ask() 的默认值必须走 placeholder 而不是 default —— questionary 的
   default 会把内容预填进可编辑缓冲区，从访达拖进来的路径会拼在旧值后面
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# 支持两种跑法：`python -m unittest discover` 与直接 `python vctl/tests/test_ui.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vctl import ui  # noqa: E402 —— 必须在 sys.path 调整之后导入


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


class _FakeQuestion:
    """假的 Question 对象：只需要 _q_ask 会调用的 unsafe_ask。"""

    def __init__(self, result=None, exc: BaseException | None = None):
        self._result = result
        self._exc = exc
        self.asked = False

    def unsafe_ask(self):
        self.asked = True
        if self._exc is not None:
            raise self._exc
        return self._result


class _FakeChoice:
    """假的 questionary.Choice，只保存 title / value 供断言。"""

    def __init__(self, title=None, value=None):
        self.title = title
        self.value = value

    def __repr__(self) -> str:  # 断言失败时打印得好看点
        return f"Choice(title={self.title!r}, value={self.value!r})"


class _FakeQuestionary:
    """最小可用的 questionary 替身。

    模拟 unsafe_ask 的返回 / 抛异常，并把每次调用的参数记在 calls 里，
    让测试能断言「到底用什么参数构造了控件」。
    """

    Choice = _FakeChoice

    def __init__(self, answer=None, exc: BaseException | None = None):
        self.answer = answer
        self.exc = exc
        self.calls: list[dict] = []

    def Style(self, spec):  # noqa: N802 —— 刻意对齐 questionary 的命名
        return spec

    def _record(self, kind: str, message: str, kwargs: dict) -> _FakeQuestion:
        self.calls.append({"kind": kind, "message": message, **kwargs})
        return _FakeQuestion(self.answer, self.exc)

    def select(self, message, **kwargs):
        return self._record("select", message, kwargs)

    def text(self, message, **kwargs):
        return self._record("text", message, kwargs)

    def press_any_key_to_continue(self, message, **kwargs):
        return self._record("press_any_key", message, kwargs)

    @property
    def last(self) -> dict:
        """最近一次调用的参数。"""
        return self.calls[-1]


def _fake_sys(stdin_tty: bool, stdout_tty: bool):
    """替身 sys 模块 —— rich_enabled 只关心 stdin/stdout 是不是终端。

    不能直接 mock.patch.object(sys.stdin, "isatty")：真实的 sys.stdin 是
    _io.TextIOWrapper，实例属性只读，patch 会抛 AttributeError。
    """
    return SimpleNamespace(
        stdin=SimpleNamespace(isatty=lambda: stdin_tty),
        stdout=SimpleNamespace(isatty=lambda: stdout_tty, flush=lambda: None),
        stderr=SimpleNamespace(flush=lambda: None),
    )


# --------------------------------------------------------------------------
# 基类：把 rich 路径的四个依赖一次性接管
# --------------------------------------------------------------------------


class _UiTestCase(unittest.TestCase):
    """公共夹具。"""

    def patch_rich(
        self,
        fake_questionary,
        *,
        plain: bool = False,
        stdin_tty: bool = True,
        stdout_tty: bool = True,
        big: bool = True,
    ):
        """让 rich_enabled() 返回 True（或按参数返回 False），并注入假 questionary。

        Returns:
            已进入的 ExitStack，调用方可以用 `with self.patch_rich(...)`: 形式。
        """
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(ui, "_load_questionary",
                                             return_value=fake_questionary))
        stack.enter_context(mock.patch.object(ui, "_terminal_big_enough",
                                             return_value=big))
        stack.enter_context(mock.patch.object(ui, "sys",
                                             _fake_sys(stdin_tty, stdout_tty)))
        # patch.dict 不带值 = 先备份再交给我们随意改动，退出时还原
        stack.enter_context(mock.patch.dict(os.environ))
        os.environ.pop("VCT_PLAIN", None)
        if plain:
            os.environ["VCT_PLAIN"] = "1"
        return stack


# --------------------------------------------------------------------------
# rich_enabled：降级条件
# --------------------------------------------------------------------------


class TestRichEnabled(_UiTestCase):
    """rich_enabled 决定整个交互层的形态，四个条件缺一不可。"""

    def test_all_conditions_met(self):
        fake = _FakeQuestionary()
        with self.patch_rich(fake):
            self.assertTrue(ui.rich_enabled())

    def test_vct_plain_escape_hatch(self):
        """VCT_PLAIN=1 是用户手里最后的逃生口，必须无条件生效。"""
        fake = _FakeQuestionary()
        with self.patch_rich(fake, plain=True):
            self.assertFalse(ui.rich_enabled())

    def test_stdin_not_tty(self):
        """`./vct < /dev/null`、管道、被当子进程拉起时走这里。"""
        fake = _FakeQuestionary()
        with self.patch_rich(fake, stdin_tty=False):
            self.assertFalse(ui.rich_enabled())

    def test_stdout_not_tty(self):
        """`./vct | cat` 时走这里。"""
        fake = _FakeQuestionary()
        with self.patch_rich(fake, stdout_tty=False):
            self.assertFalse(ui.rich_enabled())

    def test_terminal_too_small(self):
        fake = _FakeQuestionary()
        with self.patch_rich(fake, big=False):
            self.assertFalse(ui.rich_enabled())

    def test_questionary_missing(self):
        """没装 questionary 时 _load_questionary 返回 None。"""
        with self.patch_rich(None):
            self.assertFalse(ui.rich_enabled())

    def test_re_evaluated_every_call(self):
        """必须每次重新判定 —— 不能像 _COLOR_ENABLED 那样导入时定死。"""
        with self.patch_rich(_FakeQuestionary()):
            self.assertTrue(ui.rich_enabled())
            os.environ["VCT_PLAIN"] = "1"
            self.assertFalse(ui.rich_enabled())
            del os.environ["VCT_PLAIN"]
            self.assertTrue(ui.rich_enabled())


class TestTerminalBigEnough(unittest.TestCase):
    """窗口尺寸闸门。"""

    def _check(self, columns: int, lines: int) -> bool:
        size = os.terminal_size((columns, lines))
        with mock.patch.object(ui.shutil, "get_terminal_size", return_value=size):
            return ui._terminal_big_enough()

    def test_boundaries(self):
        # 阈值是 80 列 x 20 行，正好等于阈值算通过
        self.assertTrue(self._check(80, 20))
        self.assertFalse(self._check(79, 20))
        self.assertFalse(self._check(80, 19))
        self.assertTrue(self._check(200, 60))

    def test_oserror_means_fallback(self):
        with mock.patch.object(ui.shutil, "get_terminal_size",
                               side_effect=OSError):
            self.assertFalse(ui._terminal_big_enough())


class TestVersionTuple(unittest.TestCase):
    """版本号解析（用于 questionary / prompt_toolkit 的最低版本检查）。"""

    def test_normal(self):
        self.assertEqual(ui._version_tuple("2.1.1"), (2, 1, 1))
        self.assertEqual(ui._version_tuple("3.0.52"), (3, 0, 52))

    def test_comparison_semantics(self):
        self.assertLess(ui._version_tuple("1.9.9"), (2, 0))
        self.assertGreaterEqual(ui._version_tuple("2.0.0"), (2, 0))

    def test_suffix_digits_are_kept(self):
        """预发布后缀里的数字会被挑出来，不追求严格语义。

        '0rc1' 解析成 1，于是 "2.0.0rc1" 被当成 >= 2.0 —— 对这里唯一的用途
        （判断 questionary / prompt_toolkit 够不够新）是安全的方向：
        宁可放行一个 rc 版，也不该把装好的库误判成版本过低而静默降级。
        """
        self.assertEqual(ui._version_tuple("2.0.0rc1"), (2, 0, 1))
        self.assertGreaterEqual(ui._version_tuple("2.0.0rc1"), (2, 0))

    def test_garbage(self):
        self.assertEqual(ui._version_tuple(""), (0,))
        self.assertEqual(ui._version_tuple("abc"), (0,))


# --------------------------------------------------------------------------
# _safe_default / _validate_text
# --------------------------------------------------------------------------


class TestSafeDefault(unittest.TestCase):
    """本次改造最大的坑：select(default=不在候选项里的值) 会在构造时 ValueError。"""

    VALUES = ["bijian", "jianying", "whisper-api"]

    def test_empty_string_becomes_none(self):
        # ask_choice 的签名默认就是 ""，12 个调用点里没传 default 的靠这个兜住
        self.assertIsNone(ui._safe_default("", self.VALUES))

    def test_none_stays_none(self):
        self.assertIsNone(ui._safe_default(None, self.VALUES))

    def test_stale_config_value_becomes_none(self):
        """配置文件里上一版留下的值必须被收敛掉，否则 select 直接炸。"""
        self.assertIsNone(ui._safe_default("sttn-auto", self.VALUES))

    def test_hit_returns_original(self):
        self.assertEqual(ui._safe_default("jianying", self.VALUES), "jianying")

    def test_false_is_a_legitimate_default(self):
        """用 == 而不是 not default 的原因：False 是 ask_yes_no 的合法默认值。"""
        self.assertIs(ui._safe_default(False, [False, True]), False)

    def test_zero_is_a_legitimate_default(self):
        self.assertEqual(ui._safe_default("0", ["1", "0"]), "0")


class TestValidateText(unittest.TestCase):
    """文本输入的校验函数。"""

    def test_non_empty_passes(self):
        self.assertIs(ui._validate_text("素材.mp4", False, False), True)

    def test_blank_rejected_without_default(self):
        message = ui._validate_text("   ", False, False)
        self.assertIsInstance(message, str)
        self.assertIn("不能为空", message)

    def test_blank_accepted_with_default(self):
        self.assertIs(ui._validate_text("", True, False), True)

    def test_blank_accepted_when_allow_empty(self):
        self.assertIs(ui._validate_text("", False, True), True)


# --------------------------------------------------------------------------
# _q_ask：取消协议的唯一收口点
# --------------------------------------------------------------------------


class TestQAsk(unittest.TestCase):
    """Ctrl-C / Ctrl-D / 返回 None 都必须统一成 Cancelled。"""

    def test_returns_value(self):
        self.assertEqual(ui._q_ask(_FakeQuestion(result="素材.mp4")), "素材.mp4")

    def test_keyboard_interrupt(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ui.Cancelled):
                ui._q_ask(_FakeQuestion(exc=KeyboardInterrupt()))

    def test_eof(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ui.Cancelled):
                ui._q_ask(_FakeQuestion(exc=EOFError()))

    def test_runtime_error_from_asyncio(self):
        """questionary 内部走 asyncio.run()，已有事件循环时会抛 RuntimeError。"""
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ui.Cancelled):
                ui._q_ask(_FakeQuestion(exc=RuntimeError("no running event loop")))

    def test_none_is_cancelled(self):
        with self.assertRaises(ui.Cancelled):
            ui._q_ask(_FakeQuestion(result=None))

    def test_no_english_message_leaked(self):
        """用 unsafe_ask 而不是 ask 的原因：不能漏出 'Cancelled by user'。

        这里只该有我们自己补的一个换行。
        """
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(ui.Cancelled):
                ui._q_ask(_FakeQuestion(exc=KeyboardInterrupt()))
        self.assertEqual(buffer.getvalue(), "\n")
        self.assertNotIn("Cancelled", buffer.getvalue())


# --------------------------------------------------------------------------
# _q_select / ask_choice / ask_yes_no 的 rich 分支
# --------------------------------------------------------------------------


class TestSelectRichPath(_UiTestCase):
    """方向键选择分支。"""

    OPTIONS = [("1", "提取字幕 + 擦除字幕 · 推荐"), ("2", "语音转字幕（ASR） · 免费")]

    def test_returns_selected_value(self):
        fake = _FakeQuestionary(answer="1")
        with self.patch_rich(fake):
            self.assertEqual(ui.ask_choice("请选择功能", self.OPTIONS), "1")
        self.assertEqual(fake.last["kind"], "select")
        self.assertEqual(fake.last["message"], "  请选择功能")

    def test_choices_use_label_as_title(self):
        fake = _FakeQuestionary(answer="1")
        with self.patch_rich(fake):
            ui.ask_choice("请选择功能", self.OPTIONS)
        titles = [c.title for c in fake.last["choices"]]
        values = [c.value for c in fake.last["choices"]]
        self.assertEqual(titles[:2], ["提取字幕 + 擦除字幕 · 推荐", "语音转字幕（ASR） · 免费"])
        self.assertEqual(values[:2], ["1", "2"])

    def test_cancel_entry_appended_by_default(self):
        fake = _FakeQuestionary(answer="1")
        with self.patch_rich(fake):
            ui.ask_choice("请选择", self.OPTIONS)
        self.assertEqual(fake.last["choices"][-1].value, ui._CANCEL)
        self.assertIn("取消", fake.last["choices"][-1].title)

    def test_cancel_entry_omitted_for_top_menu(self):
        """主菜单自己有「退出」，再挂取消项反而让人犯嘀咕。"""
        fake = _FakeQuestionary(answer="1")
        with self.patch_rich(fake):
            ui.ask_choice("请选择功能", self.OPTIONS, allow_cancel=False)
        self.assertEqual(len(fake.last["choices"]), len(self.OPTIONS))
        self.assertNotIn(ui._CANCEL, [c.value for c in fake.last["choices"]])

    def test_selecting_cancel_raises(self):
        fake = _FakeQuestionary(answer=ui._CANCEL)
        with self.patch_rich(fake):
            with self.assertRaises(ui.Cancelled):
                ui.ask_choice("请选择", self.OPTIONS)

    def test_cancel_sentinel_is_not_none(self):
        """questionary 会把 value=None 的选项悄悄换成标题字符串，哨兵必须另想办法。"""
        self.assertIsNotNone(ui._CANCEL)
        self.assertIsInstance(ui._CANCEL, str)

    def test_valid_default_passed_through(self):
        fake = _FakeQuestionary(answer="1")
        with self.patch_rich(fake):
            ui.ask_choice("请选择", self.OPTIONS, default="2")
        self.assertEqual(fake.last["default"], "2")

    def test_stale_default_neutralised(self):
        """不收敛的话 questionary 在 select() 构造时就 ValueError。"""
        fake = _FakeQuestionary(answer="1")
        with self.patch_rich(fake):
            ui.ask_choice("请选择", self.OPTIONS, default="desub_mode")
        self.assertIsNone(fake.last["default"])

    def test_search_filter_disabled(self):
        """搜索过滤会占用 j/k，关掉才能保住 vim 式的上下移动。"""
        fake = _FakeQuestionary(answer="1")
        with self.patch_rich(fake):
            ui.ask_choice("请选择", self.OPTIONS)
        self.assertIs(fake.last["use_search_filter"], False)

    def test_keyboard_interrupt_propagates_as_cancelled(self):
        fake = _FakeQuestionary(exc=KeyboardInterrupt())
        with self.patch_rich(fake):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(ui.Cancelled):
                    ui.ask_choice("请选择", self.OPTIONS)


class TestAskChoiceFallback(_UiTestCase):
    """降级到文本输入时的行为必须和改造前**完全一致**（脚本依赖它）。"""

    OPTIONS = [("a", "甲"), ("b", "乙")]

    def _fallback(self, inputs: list[str]):
        """让 rich_enabled 返回 False，并伪造若干次输入。"""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(ui, "rich_enabled", return_value=False))
        stream = stack.enter_context(mock.patch.object(ui, "_read_line",
                                                      side_effect=inputs))
        return stack, stream

    def test_prints_numbered_list(self):
        """编号列表只在降级模式下打印；方向键模式下选项本身就是控件。"""
        stack, _ = self._fallback(["1"])
        buffer = io.StringIO()
        with stack, contextlib.redirect_stdout(buffer):
            ui.ask_choice("请选择", self.OPTIONS)
        output = buffer.getvalue()
        self.assertIn("甲", output)
        self.assertIn("乙", output)

    def test_number_input(self):
        stack, _ = self._fallback(["2"])
        with stack, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ui.ask_choice("请选择", self.OPTIONS), "b")

    def test_value_input(self):
        """脚本里 `printf 'a\\n'` 这种直接给值的用法不能坏。"""
        stack, _ = self._fallback(["a"])
        with stack, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ui.ask_choice("请选择", self.OPTIONS), "a")

    def test_blank_uses_default(self):
        stack, _ = self._fallback([""])
        with stack, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ui.ask_choice("请选择", self.OPTIONS, default="b"), "b")

    def test_invalid_then_valid(self):
        stack, _ = self._fallback(["9", "1"])
        with stack, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ui.ask_choice("请选择", self.OPTIONS), "a")

    def test_q_cancels(self):
        stack, _ = self._fallback(["q"])
        with stack, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ui.Cancelled):
                ui.ask_choice("请选择", self.OPTIONS)


class TestAskYesNoRichPath(_UiTestCase):
    """决策 4：是/否也用方向键二选一，不用 y/n 速敲。"""

    def test_yes_returns_true(self):
        fake = _FakeQuestionary(answer="yes")
        with self.patch_rich(fake):
            self.assertIs(ui.ask_yes_no("确认执行？"), True)

    def test_no_returns_false(self):
        fake = _FakeQuestionary(answer="no")
        with self.patch_rich(fake):
            self.assertIs(ui.ask_yes_no("确认执行？"), False)

    def test_choice_values_are_strings(self):
        """布尔值在 questionary 内部参与真假判断容易踩坑，所以返回后再映射。"""
        fake = _FakeQuestionary(answer="yes")
        with self.patch_rich(fake):
            ui.ask_yes_no("确认执行？")
        self.assertEqual([c.value for c in fake.last["choices"]][:2], ["yes", "no"])

    def test_default_true_preselects_yes(self):
        fake = _FakeQuestionary(answer="yes")
        with self.patch_rich(fake):
            ui.ask_yes_no("确认执行？", default=True)
        self.assertEqual(fake.last["default"], "yes")

    def test_default_false_preselects_no(self):
        """default=False 是合法默认值，不能被 _safe_default 当成「没有默认值」。"""
        fake = _FakeQuestionary(answer="no")
        with self.patch_rich(fake):
            ui.ask_yes_no("确认执行？", default=False)
        self.assertEqual(fake.last["default"], "no")

    def test_yes_no_is_cancellable(self):
        fake = _FakeQuestionary(answer=ui._CANCEL)
        with self.patch_rich(fake):
            with self.assertRaises(ui.Cancelled):
                ui.ask_yes_no("确认执行？")


class TestAskYesNoFallback(_UiTestCase):
    """降级路径的 y/n 语义。"""

    def _ask(self, text: str, default: bool = True) -> bool:
        with mock.patch.object(ui, "rich_enabled", return_value=False):
            with mock.patch.object(ui, "_read_line", return_value=text):
                return ui.ask_yes_no("确认？", default=default)

    def test_blank_uses_default(self):
        self.assertIs(self._ask("", default=True), True)
        self.assertIs(self._ask("  ", default=False), False)

    def test_yes_forms(self):
        for text in ("y", "Y", "yes", "是", "1"):
            self.assertIs(self._ask(text), True, text)

    def test_no_forms(self):
        for text in ("n", "N", "no", "否", "0"):
            self.assertIs(self._ask(text), False, text)

    def test_invalid_then_cancel(self):
        with mock.patch.object(ui, "rich_enabled", return_value=False):
            with mock.patch.object(ui, "_read_line", side_effect=["随便", "q"]):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(ui.Cancelled):
                        ui.ask_yes_no("确认？")


# --------------------------------------------------------------------------
# ask() 的 rich 分支：placeholder 而非 default
# --------------------------------------------------------------------------


class TestAskRichPath(_UiTestCase):
    """默认值必须走 placeholder —— 这是拖拽路径会踩的坑。"""

    def test_default_goes_to_placeholder_not_buffer(self):
        fake = _FakeQuestionary(answer="")
        with self.patch_rich(fake):
            ui.ask("请输入视频路径", default="/旧/素材.mp4")
        call = fake.last
        self.assertEqual(call["kind"], "text")
        self.assertIn("/旧/素材.mp4", call["placeholder"])
        # 关键：不能传 default —— 那会把旧值预填进可编辑缓冲区，
        # 用户从访达拖进来的路径就拼在后面变成「/旧/素材.mp4/新文件.mp4」
        self.assertNotIn("default", call)

    def test_blank_returns_default(self):
        fake = _FakeQuestionary(answer="")
        with self.patch_rich(fake):
            self.assertEqual(ui.ask("路径", default="/旧/素材.mp4"), "/旧/素材.mp4")

    def test_typed_value_wins(self):
        fake = _FakeQuestionary(answer="  /新/素材.mp4  ")
        with self.patch_rich(fake):
            self.assertEqual(ui.ask("路径", default="/旧/素材.mp4"), "/新/素材.mp4")

    def test_no_default_means_no_placeholder(self):
        fake = _FakeQuestionary(answer="abc")
        with self.patch_rich(fake):
            ui.ask("名称")
        self.assertEqual(fake.last["placeholder"], "")

    def test_validate_wired_to_validate_text(self):
        fake = _FakeQuestionary(answer="x")
        with self.patch_rich(fake):
            ui.ask("路径")
            validate = fake.last["validate"]
        # 没有默认值、也不允许空 -> 空输入被拦下
        self.assertIsInstance(validate(""), str)
        self.assertIs(validate(" 素材.mp4 "), True)

    def test_validate_allows_blank_with_default(self):
        fake = _FakeQuestionary(answer="x")
        with self.patch_rich(fake):
            ui.ask("路径", default="/a.mp4")
            validate = fake.last["validate"]
        self.assertIs(validate(""), True)

    def test_text_question_has_no_instruction_bar(self):
        """文本题不挂那排英文操作提示。"""
        fake = _FakeQuestionary(answer="x")
        with self.patch_rich(fake):
            ui.ask("路径")
        self.assertIsNone(fake.last["instruction"])

    def test_q_still_cancels(self):
        fake = _FakeQuestionary(answer="q")
        with self.patch_rich(fake):
            with self.assertRaises(ui.Cancelled):
                ui.ask("路径")


class TestAskFallback(_UiTestCase):
    """降级路径的文本输入。"""

    def _ask(self, inputs: list[str], **kwargs):
        with mock.patch.object(ui, "rich_enabled", return_value=False):
            with mock.patch.object(ui, "_read_line", side_effect=inputs):
                with contextlib.redirect_stdout(io.StringIO()):
                    return ui.ask("路径", **kwargs)

    def test_returns_stripped_input(self):
        self.assertEqual(self._ask(["  /a/b.mp4  "]), "/a/b.mp4")

    def test_blank_uses_default(self):
        self.assertEqual(self._ask([""], default="/a.mp4"), "/a.mp4")

    def test_blank_retries_when_required(self):
        """没有默认值又不允许空 —— 重问，和改造前一致。"""
        self.assertEqual(self._ask(["", "  ", "/b.mp4"]), "/b.mp4")

    def test_allow_empty(self):
        self.assertEqual(self._ask([""], allow_empty=True), "")

    def test_q_cancels(self):
        with self.assertRaises(ui.Cancelled):
            self._ask(["q"])


# --------------------------------------------------------------------------
# pause：绝不能向外抛异常
# --------------------------------------------------------------------------


class TestPause(_UiTestCase):
    """menu.py 的主循环靠它歇一口气，抛异常会把整个循环带崩。"""

    def test_rich_path_swallows_interrupts(self):
        for exc in (KeyboardInterrupt(), EOFError(), RuntimeError()):
            fake = _FakeQuestionary(exc=exc)
            with self.patch_rich(fake), contextlib.redirect_stdout(io.StringIO()):
                ui.pause()  # 不该抛 Cancelled / KeyboardInterrupt

    def test_rich_path_uses_questionary(self):
        fake = _FakeQuestionary(answer=None)
        with self.patch_rich(fake):
            ui.pause("按任意键返回菜单")
        self.assertEqual(fake.last["kind"], "press_any_key")
        self.assertIn("按任意键返回菜单", fake.last["message"])

    def test_fallback_path_swallows_interrupts(self):
        with mock.patch.object(ui, "rich_enabled", return_value=False), \
                mock.patch.object(ui, "_read_line", side_effect=KeyboardInterrupt()), \
                contextlib.redirect_stdout(io.StringIO()):
            ui.pause()

    def test_fallback_default_prompt(self):
        # 降级路径把提示语交给 input() 当参数打出（而不是自己 print），
        # 所以这里断言的是传给 input 的那段文字。
        with mock.patch.object(ui, "rich_enabled", return_value=False), \
                mock.patch.object(ui, "input", create=True, return_value="") as fake_input:
            ui.pause()
        self.assertIn("按任意键", fake_input.call_args.args[0])


# --------------------------------------------------------------------------
# 纯函数回归
# --------------------------------------------------------------------------


class TestIsCancel(unittest.TestCase):
    """取消词。"""

    def test_cancel_words(self):
        for text in ("q", "Q", " quit ", "EXIT", "退出"):
            self.assertTrue(ui._is_cancel(text), text)

    def test_normal_input_is_not_cancel(self):
        for text in ("", "1", "素材.mp4", "queen"):
            self.assertFalse(ui._is_cancel(text), text)


class TestCleanPath(unittest.TestCase):
    r"""macOS 拖拽路径清洗 —— 改造交互层不该影响它。

    反斜杠反转义是 POSIX 专属行为（Windows 的 `\` 是路径分隔符），
    所以这类用例必须钉住 os.name 再跑，不能随当前平台漂移。
    注意替换的是 ui 模块里的 os 引用 —— 直接 patch 全局 os.name
    会让 pathlib 在实例化时读到假平台而炸掉。
    """

    @staticmethod
    def _patch_platform(name: str):
        # clean_path 只用到 os.name 和 os.path.expanduser
        return mock.patch.object(ui, "os", SimpleNamespace(name=name, path=os.path))

    def test_drag_escaped_space(self):
        with self._patch_platform("posix"):
            self.assertEqual(ui.clean_path(r"/Users/me/my\ file.mp4"),
                             "/Users/me/my file.mp4")

    def test_quoted_path(self):
        self.assertEqual(ui.clean_path("'/Users/me/my file.mp4'"),
                         "/Users/me/my file.mp4")

    def test_double_quoted_path(self):
        self.assertEqual(ui.clean_path('"/Users/me/a.mp4"'), "/Users/me/a.mp4")

    def test_double_wrapped_quotes(self):
        self.assertEqual(ui.clean_path("""'" /a b/c.mp4 "'"""), "/a b/c.mp4")

    def test_escaped_parens(self):
        # 有些终端会把 () 一起转义
        with self._patch_platform("posix"):
            self.assertEqual(ui.clean_path(r"/tmp/素材\(1\).mp4"), "/tmp/素材(1).mp4")

    def test_windows_path_backslashes_kept(self):
        # Windows 的 `\` 是分隔符不是转义符：剥掉会把绝对路径碾成
        # 盘符相对路径（E:a\b.mp4 -> E:ab.mp4），resolve() 拼到当前目录下。
        with self._patch_platform("nt"):
            self.assertEqual(ui.clean_path(r"E:\带货\素材\视频 1.mp4"),
                             r"E:\带货\素材\视频 1.mp4")

    def test_windows_quoted_path_backslashes_kept(self):
        with self._patch_platform("nt"):
            self.assertEqual(ui.clean_path(r'"D:\clips\a.mp4"'), r"D:\clips\a.mp4")

    def test_tilde_expanded(self):
        expected = os.path.expanduser("~/Desktop/素材.mp4")
        self.assertEqual(ui.clean_path('"~/Desktop/素材.mp4"'), expected)

    def test_chinese_and_spaces_untouched(self):
        self.assertEqual(ui.clean_path("/Users/me/带货 素材/视频 1.mp4"),
                         "/Users/me/带货 素材/视频 1.mp4")

    def test_empty(self):
        self.assertEqual(ui.clean_path(""), "")
        self.assertEqual(ui.clean_path("   "), "")
        self.assertEqual(ui.clean_path(None), "")


class TestHumanSize(unittest.TestCase):
    """字节数格式化。"""

    def test_units(self):
        self.assertEqual(ui.human_size(0), "0 B")
        self.assertEqual(ui.human_size(1023), "1023 B")
        self.assertEqual(ui.human_size(1024), "1.0 KB")
        self.assertEqual(ui.human_size(1536), "1.5 KB")
        self.assertEqual(ui.human_size(1024 ** 2), "1.0 MB")


class TestDescribeFile(unittest.TestCase):
    """文件描述行。"""

    def test_missing_file(self):
        text = ui.describe_file(Path("/definitely/not/here.mp4"))
        self.assertIn("未生成", text)

    def test_existing_file(self):
        text = ui.describe_file(Path(__file__))
        self.assertIn("test_ui.py", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
