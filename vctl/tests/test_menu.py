"""
vctl/menu.py 的单元测试。

菜单是用户唯一会看到的界面，最容易出的错是「编号和 handler 对不上」
—— 这种错平时不报，只有在用户点进去的那一刻才炸。所以这里重点盯：

1. _menu_items() 的候选项与 MENU_ITEMS 严格一致（编号集合、标签拼法）
2. 每个 handler 名字都能真的解析成可调用对象（提前抓出拼写错误）
3. 顶层主菜单不挂「取消」项（它自己有「退出」）
"""

from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vctl import menu, ui  # noqa: E402


class TestMenuItems(unittest.TestCase):
    """主菜单候选项。"""

    def test_has_exit_at_the_end(self):
        items = menu._menu_items()
        self.assertEqual(items[-1], ("0", "退出"))

    def test_count_matches_menu_items(self):
        self.assertEqual(len(menu._menu_items()), len(menu.MENU_ITEMS) + 1)

    def test_values_are_unique(self):
        """重复的值会让 lookup 字典静默丢项 —— 用户选了却进不去。"""
        values = [value for value, _ in menu._menu_items()]
        self.assertEqual(len(values), len(set(values)))

    def test_value_set_matches_menu_items(self):
        self.assertEqual(
            {value for value, _ in menu._menu_items()},
            {number for number, _, _, _ in menu.MENU_ITEMS} | {"0"},
        )

    def test_labels_merge_title_and_subtitle(self):
        """决策 3：副标题并进标题行 —— questionary 的选项只支持单行标题。"""
        by_value = dict(menu._menu_items())
        for number, title, subtitle, _ in menu.MENU_ITEMS:
            self.assertEqual(by_value[number], f"{title} · {subtitle}")

    def test_labels_have_no_numbering(self):
        """决策 5：去掉编号。方向键选择下编号只会干扰视线。"""
        for value, label in menu._menu_items():
            if value == "0":
                continue
            self.assertFalse(label.startswith(f"{value})"), label)
            self.assertFalse(label.startswith(f"{value}."), label)

    def test_labels_are_single_line(self):
        """多行标签会把 questionary 的选项框撑坏。"""
        for _, label in menu._menu_items():
            self.assertNotIn("\n", label)


class TestResolve(unittest.TestCase):
    """handler 字符串 -> 可调用对象。"""

    def test_special_case_config_menu(self):
        self.assertIs(menu._resolve("config_menu"), menu.config_menu)

    def test_special_case_vc_gui(self):
        from vctl.commands import vc

        self.assertIs(menu._resolve("vc_gui"), vc.launch_gui)

    def test_dotted_name(self):
        handler = menu._resolve("combo.interactive_extract_desub")
        self.assertTrue(callable(handler))
        self.assertEqual(handler.__name__, "interactive_extract_desub")

    def test_every_menu_item_resolves(self):
        """任何一项写错名字都会在用户点下去的那一刻才炸，这里提前拦住。"""
        for number, title, _, handler_name in menu.MENU_ITEMS:
            with self.subTest(number=number, title=title):
                self.assertTrue(callable(menu._resolve(handler_name)),
                                f"{title} -> {handler_name}")

    def test_bad_module_raises(self):
        with self.assertRaises(ModuleNotFoundError):
            menu._resolve("no_such_module.nope")


class TestLookupIntegrity(unittest.TestCase):
    """main_menu 内部的 编号 -> handler 映射。"""

    def test_lookup_covers_all_items(self):
        lookup = {number: handler for number, _, _, handler in menu.MENU_ITEMS}
        self.assertEqual(len(lookup), len(menu.MENU_ITEMS))
        self.assertNotIn("0", lookup)  # 「退出」必须在查表之前就被处理掉


class TestMainMenuFlow(unittest.TestCase):
    """主循环的分支：退出、取消、非法选项、handler 抛异常。"""

    @contextlib.contextmanager
    def _silence(self):
        """屏蔽菜单的屏幕输出，并把造出来的 mock 交给测试断言。

        主循环会一直喊「再见。」，不拦掉的话测试结果的汇总行都被刷没了。

        Yields:
            {名字: mock} —— warn / error / pause / ask_choice 四个常用断言点。
        """
        mocks = {
            name: mock.MagicMock() for name in
            ("blank", "info", "rule", "warn", "error", "hint", "pause")
        }
        with contextlib.ExitStack() as stack:
            for name in ("_draw_header", "_status_line", "_last_file_line"):
                stack.enter_context(mock.patch.object(menu, name, return_value=""))
            for name, mocker in mocks.items():
                stack.enter_context(mock.patch.object(ui, name, mocker))
            # 状态行会真去探测环境，这里换成一秒返回的假数据
            for probe in ("probe_videocaptioner", "probe_vsr", "probe_ffmpeg",
                          "probe_scenedetect"):
                stack.enter_context(mock.patch.object(
                    menu.env, probe, return_value=mock.Mock(ok=True)))
            yield mocks

    def _run(self, choices: list[str], cancel_at: int | None = None,
             handler=lambda: 0):
        """跑一次 main_menu，按给定顺序「选中」选项。

        Args:
            choices: 依次选中的值。
            cancel_at: 第几次提问时抛 Cancelled（0 表示第一次）。
            handler: _resolve 返回的处理函数。
        Returns:
            (main_menu 的返回值, mock 字典)。
        """
        calls: list[int] = []

        def fake_ask_choice(prompt, options, **kwargs):
            index = len(calls)
            calls.append(index)
            if cancel_at is not None and index == cancel_at:
                raise ui.Cancelled()
            return choices[index]

        with self._silence() as mocks:
            ask = mocks["ask_choice"] = mock.MagicMock(side_effect=fake_ask_choice)
            with mock.patch.object(ui, "ask_choice", ask), \
                    mock.patch.object(menu, "_resolve", return_value=handler):
                return menu.main_menu(), mocks

    def test_exit_returns_zero(self):
        code, _ = self._run(["0"])
        self.assertEqual(code, 0)

    def test_ctrl_c_returns_zero(self):
        code, _ = self._run([], cancel_at=0)
        self.assertEqual(code, 0)

    def test_unknown_choice_loops_back(self):
        """降级到文本输入时，用户可能敲出候选之外的东西。"""
        code, mocks = self._run(["99", "0"])
        self.assertEqual(code, 0)
        self.assertTrue(mocks["warn"].called)

    def test_handler_exception_does_not_kill_menu(self):
        """单个功能崩了不该把整个菜单带崩。"""
        def boom():
            raise RuntimeError("上游炸了")

        code, mocks = self._run(["1", "0"], handler=boom)
        self.assertEqual(code, 0)
        self.assertTrue(mocks["error"].called)
        self.assertTrue(mocks["pause"].called)  # 出错也要让用户看完再回菜单

    def test_handler_cancelled_returns_to_menu(self):
        def cancel():
            raise ui.Cancelled()

        code, mocks = self._run(["1", "0"], handler=cancel)
        self.assertEqual(code, 0)
        self.assertFalse(mocks["error"].called)  # 主动取消不算错误

    def test_top_menu_has_no_cancel_entry(self):
        """顶层已经有「退出」，再挂一个「← 取消 / 返回」会让人犯嘀咕。"""
        _, mocks = self._run(["0"])
        self.assertIs(mocks["ask_choice"].call_args.kwargs.get("allow_cancel"), False)


class TestConfigMenu(unittest.TestCase):
    """配置管理子菜单。"""

    def test_edit_one_uses_config_key_as_value(self):
        """候选项的值直接是配置键 —— 选错项的可能性归零，也不用再校验序号。"""
        captured: dict = {}

        def fake_ask_choice(prompt, options, **kwargs):
            captured["options"] = options
            raise ui.Cancelled()  # 拿到选项就撤，不真的去改配置

        with mock.patch.object(ui, "ask_choice", side_effect=fake_ask_choice), \
                mock.patch.object(ui, "blank"), \
                mock.patch.object(ui, "hint"):
            menu._edit_one()

        values = [value for value, _ in captured["options"]]
        self.assertEqual(values, [key for key, _, _ in menu.EDITABLE_CONFIG])

    def test_editable_config_keys_are_unique(self):
        keys = [key for key, _, _ in menu.EDITABLE_CONFIG]
        self.assertEqual(len(keys), len(set(keys)))

    def test_config_menu_exit(self):
        with mock.patch.object(ui, "header"), \
                mock.patch.object(ui, "section"), \
                mock.patch.object(ui, "kv_table"), \
                mock.patch.object(ui, "blank"), \
                mock.patch.object(ui, "info"), \
                mock.patch.object(ui, "ask_choice", side_effect=["0"]) as ask:
            self.assertEqual(menu.config_menu(), 0)
        values = [value for value, _ in ask.call_args.args[1]]
        self.assertEqual(values, ["1", "2", "3", "0"])


class TestStatusLine(unittest.TestCase):
    """顶部的环境状态行。"""

    def test_marks_and_hint(self):
        with mock.patch.object(menu.env, "probe_videocaptioner",
                               return_value=mock.Mock(ok=True)), \
                mock.patch.object(menu.env, "probe_vsr",
                                  return_value=mock.Mock(ok=False)), \
                mock.patch.object(menu.env, "probe_ffmpeg",
                                  return_value=mock.Mock(ok=True)):
            line = menu._status_line()
        self.assertIn("字幕功能", line)
        self.assertIn("去字幕", line)
        self.assertIn("镜头分割", line)
        self.assertIn("环境诊断", line)  # 提示语不再写死「选 11」

    def test_no_hint_when_all_ready(self):
        ready = mock.Mock(ok=True)
        with contextlib.ExitStack() as stack:
            for probe in ("probe_videocaptioner", "probe_vsr", "probe_ffmpeg",
                          "probe_scenedetect"):
                stack.enter_context(
                    mock.patch.object(menu.env, probe, return_value=ready))
            line = menu._status_line()
        self.assertNotIn("未就绪", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
