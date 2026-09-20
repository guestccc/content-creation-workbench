"""
vctl/commands/scene.py 的单元测试。

镜头分割有两个地方最容易悄悄错，而且错了不会报错、只会给出坏结果：

1. **命令拼错** —— scenedetect 的全局选项（-i/-o/-m）必须排在检测子命令
   之前；`-m` 的值不带 `s` 后缀会被当成「帧数」而不是「秒数」。这两条都
   不会有任何提示，只会让检测结果莫名偏多或偏少。
2. **CSV 解析错** —— 列名、BOM、空清单、单镜头，每一种都可能让切点算错，
   而错算出来的时间轴照样能切出文件，只是切在错的地方。

所以这里重点盯这两块。切割命令另有一个被实测逼出来的约束：默认必须重编码
（`-c copy` 会把片段起点吸附到关键帧上），这条也钉在测试里。

std-lib unittest，不用 pytest（见 vctl/tests/__init__.py）。
"""

from __future__ import annotations

import contextlib
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vctl import ui  # noqa: E402
from vctl.commands import scene  # noqa: E402

# PySceneDetect 写出的真实表头（0.7.1，list-scenes --skip-cuts）
HEADER = [
    "Scene Number",
    "Start Frame",
    "Start Timecode",
    "Start Time (seconds)",
    "End Frame",
    "End Timecode",
    "End Time (seconds)",
    "Length (frames)",
    "Length (timecode)",
    "Length (seconds)",
]


def write_scenes_csv(path: Path, rows: list[tuple], encoding: str = "utf-8", bom: bool = False) -> Path:
    """写一个场景 CSV。rows 是 (序号, 起始帧, 起始时间码, 起始秒, 结束帧, 结束时间码, 结束秒, 帧数, 时长码, 时长秒)。"""
    with path.open("w", encoding=encoding, newline="") as handle:
        if bom:
            handle.write("﻿")
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for row in rows:
            writer.writerow(row)
    return path


def row(number: int, start: float, end: float, fps: int = 30) -> tuple:
    """按给定秒数造一行，帧号和时间码由 fps 推出来。"""
    def tc(seconds: float) -> str:
        total = int(round(seconds * 1000))
        h, rem = divmod(total, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    return (
        number,
        int(round(start * fps)), tc(start), f"{start:.3f}",
        int(round(end * fps)), tc(end), f"{end:.3f}",
        int(round((end - start) * fps)), tc(end - start), f"{end - start:.3f}",
    )


class TestFormatTimecode(unittest.TestCase):
    """秒 → HH:MM:SS.mmm。"""

    def test_zero(self):
        self.assertEqual(scene._format_timecode(0), "00:00:00.000")

    def test_milliseconds_kept(self):
        self.assertEqual(scene._format_timecode(1.733), "00:00:01.733")

    def test_rolls_over_minute_and_hour(self):
        self.assertEqual(scene._format_timecode(3661.5), "01:01:01.500")

    def test_negative_clamped(self):
        """负时间戳在 ffmpeg 里是真实存在的东西，不能让它渲染成 -1:-1。"""
        self.assertEqual(scene._format_timecode(-0.5), "00:00:00.000")

    def test_rounds_to_nearest_millisecond(self):
        self.assertEqual(scene._format_timecode(1.0006), "00:00:01.001")


class TestDisplayWidth(unittest.TestCase):
    """含中文的表格要靠显示宽度对齐，不能用 len()。"""

    def test_ascii_is_one_column_each(self):
        self.assertEqual(scene._display_width("abc"), 3)

    def test_cjk_is_two_columns(self):
        self.assertEqual(scene._display_width("开始"), 4)

    def test_mixed(self):
        # 时长(4) + 空格(1) + 1.5(3) + s(1) = 9
        self.assertEqual(scene._display_width("时长 1.5s"), 9)

    def test_pad_uses_display_width(self):
        """「开始」和「abcd」显示宽度都是 4，补出来的空格数应该一样。"""
        self.assertEqual(scene._pad("开始", 6), "开始  ")
        self.assertEqual(scene._pad("abcd", 6), "abcd  ")

    def test_pad_right_align(self):
        self.assertEqual(scene._pad("1", 4, "right"), "   1")


class TestParseScenesCsv(unittest.TestCase):
    """场景 CSV → Scene 列表。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_parses_multiple_scenes(self):
        path = write_scenes_csv(
            self.tmp / "a.csv",
            [row(1, 0.0, 1.733), row(2, 1.733, 3.333), row(3, 3.333, 4.933)],
        )
        scenes = scene._parse_scenes_csv(path)
        self.assertEqual(len(scenes), 3)
        self.assertEqual([s.number for s in scenes], [1, 2, 3])
        self.assertAlmostEqual(scenes[0].start, 0.0, places=3)
        self.assertAlmostEqual(scenes[1].start, 1.733, places=3)
        self.assertAlmostEqual(scenes[2].end, 4.933, places=3)

    def test_duration_is_derived(self):
        path = write_scenes_csv(self.tmp / "a.csv", [row(1, 1.0, 3.5)])
        self.assertAlmostEqual(scene._parse_scenes_csv(path)[0].duration, 2.5, places=3)

    def test_single_scene_means_no_cuts(self):
        """整条视频一个镜头 —— 这是合法结果，不是错误。"""
        path = write_scenes_csv(self.tmp / "a.csv", [row(1, 0.0, 115.0)])
        scenes = scene._parse_scenes_csv(path)
        self.assertEqual(len(scenes), 1)
        self.assertAlmostEqual(scenes[0].duration, 115.0, places=3)

    def test_empty_csv_gives_empty_list(self):
        path = write_scenes_csv(self.tmp / "a.csv", [])
        self.assertEqual(scene._parse_scenes_csv(path), [])

    def test_handles_utf8_bom(self):
        """带 BOM 时首列名会变成 '\\ufeffScene Number'，utf-8-sig 就是为它准备的。"""
        path = write_scenes_csv(self.tmp / "a.csv", [row(1, 0.0, 2.0)], bom=True)
        self.assertEqual(len(scene._parse_scenes_csv(path)), 1)

    def test_missing_column_raises(self):
        path = self.tmp / "bad.csv"
        path.write_text("Scene Number,Start Frame\n1,0\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            scene._parse_scenes_csv(path)

    def test_non_numeric_value_raises(self):
        """「Start Time (seconds)」列脏了必须炸，不能当成 0 —— 那会把镜头切在开头。"""
        path = self.tmp / "bad.csv"
        path.write_text(
            ",".join(HEADER) + "\n"
            "1,0,00:00:00.000,不是数字,60,00:00:02.000,2.000,60,00:00:02.000,2.000\n",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            scene._parse_scenes_csv(path)

    def test_row_count_mismatch_raises(self):
        """少一列的行也是脏数据，同样不能静默跳过。"""
        path = self.tmp / "short.csv"
        path.write_text(",".join(HEADER) + "\n1,0,00:00:00.000\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            scene._parse_scenes_csv(path)


class TestFindScenesCsv(unittest.TestCase):
    """PySceneDetect 的产物文件名带视频名，所以按后缀找而不是拼名字。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_dir_returns_none(self):
        self.assertIsNone(scene._find_scenes_csv(self.tmp))

    def test_finds_the_csv(self):
        target = write_scenes_csv(self.tmp / "带 空格 的中文名-Scenes.csv", [row(1, 0.0, 1.0)])
        self.assertEqual(scene._find_scenes_csv(self.tmp), target)

    def test_ignores_non_csv(self):
        (self.tmp / "log.txt").write_text("x", encoding="utf-8")
        self.assertIsNone(scene._find_scenes_csv(self.tmp))


class TestBuildDetectCommand(unittest.TestCase):
    """scenedetect 的命令拼装。参数顺序错了它不报错，只是结果不对。"""

    def _cmd(self, detector="adaptive", threshold=None, min_len=0.6):
        return scene._build_detect_command(
            "/usr/local/bin/scenedetect",
            Path("/v/素材.mp4"),
            Path("/tmp/work"),
            detector,
            threshold,
            min_len,
        )

    def test_global_options_come_before_subcommand(self):
        """-i/-o/-q/-m 是全局选项，必须排在 detect-* 之前，否则 scenedetect 直接报参数错。"""
        cmd = self._cmd()
        detect_at = cmd.index("detect-adaptive")
        for flag in ("-i", "-o", "-q", "-m"):
            self.assertLess(cmd.index(flag), detect_at, f"{flag} 排到了子命令后面")

    def test_min_len_carries_seconds_suffix(self):
        """不带 s 会被当成帧数：-m 1 是 1 帧，-m 1s 才是 1 秒。"""
        cmd = self._cmd(min_len=1.0)
        self.assertEqual(cmd[cmd.index("-m") + 1], "1.0s")

    def test_threshold_omitted_when_none(self):
        """不传就交给 PySceneDetect 用它自己的默认值，我们不去复制一份默认表。"""
        self.assertNotIn("--threshold", self._cmd())

    def test_threshold_passed_when_given(self):
        cmd = self._cmd(threshold=5.0)
        self.assertEqual(cmd[cmd.index("--threshold") + 1], "5.0")

    def test_detector_selects_subcommand(self):
        for detector in ("adaptive", "content", "threshold"):
            self.assertIn(f"detect-{detector}", self._cmd(detector=detector))

    def test_quiet_and_skip_cuts(self):
        """-q 挡掉 tqdm 进度条；--skip-cuts 让 CSV 成为标准表格好解析。"""
        cmd = self._cmd()
        self.assertIn("-q", cmd)
        self.assertEqual(cmd[-2:], ["list-scenes", "--skip-cuts"])

    def test_paths_are_strings(self):
        cmd = self._cmd()
        for part in cmd:
            self.assertIsInstance(part, str)


class TestBuildSplitCommand(unittest.TestCase):
    """ffmpeg 切割命令。"""

    def _cmd(self, copy_mode: bool, start=1.733, duration=1.600):
        target = Path("/out/clip.mp4")
        return scene._build_split_command(
            "/usr/bin/ffmpeg",
            Path("/v/素材.mp4"),
            scene.Scene(number=2, start=start, end=start + duration),
            target,
            copy_mode,
        )

    def test_input_seek_precedes_input(self):
        """-ss 放 -i 前是输入定位，慢片源上快得多；重编码下它依然是精确的。"""
        cmd = self._cmd(copy_mode=False)
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))

    def test_duration_applied_after_input(self):
        cmd = self._cmd(copy_mode=False)
        self.assertGreater(cmd.index("-t"), cmd.index("-i"))
        self.assertEqual(cmd[cmd.index("-t") + 1], "1.600")

    def test_default_re_encodes(self):
        """默认必须重编码 —— 关键帧间隔大的片源用 copy 会切出错误时长。"""
        cmd = self._cmd(copy_mode=False)
        self.assertIn("libx264", cmd)
        self.assertNotIn("copy", cmd)

    def test_re_encode_pins_quality_settings(self):
        cmd = self._cmd(copy_mode=False)
        self.assertEqual(cmd[cmd.index("-crf") + 1], scene.VIDEO_CRF)
        self.assertEqual(cmd[cmd.index("-preset") + 1], scene.VIDEO_PRESET)

    def test_copy_mode_uses_stream_copy(self):
        cmd = self._cmd(copy_mode=True)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        # 复制模式下负时间戳会让部分播放器花屏或没声音
        self.assertIn("-avoid_negative_ts", cmd)

    def test_target_is_last(self):
        self.assertEqual(self._cmd(copy_mode=False)[-1], "/out/clip.mp4")


class TestExportCsv(unittest.TestCase):
    """切点清单导出。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip(self):
        scenes = [scene.Scene(1, 0.0, 1.733), scene.Scene(2, 1.733, 3.333)]
        target = self.tmp / "out.csv"
        self.assertTrue(scene._export_csv(scenes, target))

        with target.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["序号"], "1")
        self.assertEqual(rows[1]["开始时间码"], "00:00:01.733")
        self.assertEqual(rows[1]["时长(秒)"], "1.600")

    def test_unwritable_path_returns_false(self):
        target = self.tmp / "没有这个目录" / "out.csv"
        with mock.patch.object(ui, "error"):
            self.assertFalse(scene._export_csv([scene.Scene(1, 0.0, 1.0)], target))


class TestRunExitCodes(unittest.TestCase):
    """run() 的退出码约定：0 成功 / 2 参数错误 / 3 输入不存在 / 4 依赖缺失 / 5 运行时失败。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.video = self.tmp / "素材.mp4"
        self.video.write_bytes(b"\x00" * 64)

    def tearDown(self):
        self._tmp.cleanup()

    def _args(self, **overrides) -> SimpleNamespace:
        base = dict(
            input=str(self.video),
            split=False,
            output_dir=None,
            detector="adaptive",
            threshold=None,
            min_len=0.6,
            copy=False,
            csv=False,
            dry_run=False,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    @contextlib.contextmanager
    def _silence(self):
        """拦掉屏幕输出，避免把测试汇总行刷没。

        镜头表格走的是裸 print（要自己算列宽对齐），所以这里连 stdout
        一起接住 —— 不拦的话 29 行表格会把测试结果刷得看不见。
        """
        names = ("header", "section", "kv_table", "step", "hint", "info",
                 "ok", "warn", "error", "blank", "rule")
        with contextlib.ExitStack() as stack:
            mocks = {}
            for name in names:
                mocks[name] = stack.enter_context(
                    mock.patch.object(ui, name, mock.MagicMock()))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            yield mocks

    def _probe(self, ok=True):
        return mock.Mock(ok=ok, path=Path("/usr/local/bin/scenedetect"),
                         detail="", fix="", name="PySceneDetect")

    def test_missing_input_returns_3(self):
        args = self._args(input=str(self.tmp / "不存在.mp4"))
        with self._silence():
            self.assertEqual(scene.run(args), 3)

    def test_directory_as_input_returns_3(self):
        args = self._args(input=str(self.tmp))
        with self._silence():
            self.assertEqual(scene.run(args), 3)

    def test_bad_min_len_returns_2(self):
        with self._silence():
            self.assertEqual(scene.run(self._args(min_len=0)), 2)
            self.assertEqual(scene.run(self._args(min_len=-1)), 2)

    def test_missing_scenedetect_returns_4(self):
        with self._silence(), \
                mock.patch.object(scene.env, "probe_scenedetect",
                                  return_value=self._probe(ok=False)), \
                mock.patch.object(scene, "ensure", return_value=False):
            self.assertEqual(scene.run(self._args()), 4)

    def test_detection_failure_returns_5(self):
        with self._silence(), \
                mock.patch.object(scene.env, "probe_scenedetect",
                                  return_value=self._probe()), \
                mock.patch.object(scene, "ensure", return_value=True), \
                mock.patch.object(scene, "_detect", return_value=None):
            self.assertEqual(scene.run(self._args()), 5)

    def test_empty_detection_result_is_a_failure(self):
        """一行都没解析出来 = 没读懂检测结果，不是「没有切点」，不能当成成功。

        PySceneDetect 对一条正常视频至少会报一个场景，所以空清单只可能来自
        坏产物；真正的单镜头走 test_single_scene_* 那条路。
        """
        with self._silence() as mocks, \
                mock.patch.object(scene.env, "probe_scenedetect",
                                  return_value=self._probe()), \
                mock.patch.object(scene, "ensure", return_value=True), \
                mock.patch.object(scene, "_detect", return_value=[]):
            self.assertEqual(scene.run(self._args()), 5)
            self.assertTrue(mocks["error"].called)

    def test_single_scene_warns_and_still_succeeds(self):
        """单镜头素材是合法结果：要提示，但退出码仍是 0。"""
        scenes = [scene.Scene(1, 0.0, 115.0)]
        with self._silence() as mocks, \
                mock.patch.object(scene.env, "probe_scenedetect",
                                  return_value=self._probe()), \
                mock.patch.object(scene, "ensure", return_value=True), \
                mock.patch.object(scene, "_detect", return_value=scenes):
            self.assertEqual(scene.run(self._args()), 0)
            self.assertTrue(mocks["warn"].called)
            self.assertTrue(any("只有一个镜头" in str(call)
                                for call in mocks["warn"].call_args_list))

    def test_single_scene_skips_splitting(self):
        """单镜头切出来就是原片，白跑一遍重编码没有意义。"""
        scenes = [scene.Scene(1, 0.0, 115.0)]
        out = self.tmp / "切好的"
        with self._silence(), \
                mock.patch.object(scene.env, "probe_scenedetect",
                                  return_value=self._probe()), \
                mock.patch.object(scene, "ensure", return_value=True), \
                mock.patch.object(scene, "_detect", return_value=scenes), \
                mock.patch.object(scene, "_split_scenes") as split:
            self.assertEqual(scene.run(self._args(split=True, output_dir=str(out))), 0)
            split.assert_not_called()
            self.assertFalse((self.tmp / "素材_scenes").exists())

    def test_list_only_writes_nothing(self):
        """默认只列清单，不该建输出目录、不该调切割。"""
        scenes = [scene.Scene(1, 0.0, 2.0), scene.Scene(2, 2.0, 4.0)]
        with self._silence(), \
                mock.patch.object(scene.env, "probe_scenedetect",
                                  return_value=self._probe()), \
                mock.patch.object(scene, "ensure", return_value=True), \
                mock.patch.object(scene, "_detect", return_value=scenes), \
                mock.patch.object(scene, "_split_scenes") as split:
            self.assertEqual(scene.run(self._args()), 0)
            split.assert_not_called()
            self.assertFalse((self.tmp / "素材_scenes").exists())

    def test_split_writes_into_output_dir(self):
        scenes = [scene.Scene(1, 0.0, 2.0), scene.Scene(2, 2.0, 4.0)]
        out = self.tmp / "切好的"
        with self._silence(), \
                mock.patch.object(scene.env, "probe_scenedetect",
                                  return_value=self._probe()), \
                mock.patch.object(scene, "ensure", return_value=True), \
                mock.patch.object(scene, "_detect", return_value=scenes), \
                mock.patch.object(scene, "_split_scenes", return_value=(2, 0)) as split:
            self.assertEqual(scene.run(self._args(split=True, output_dir=str(out))), 0)
            self.assertTrue(out.is_dir())
            # macOS 上 /var 是 /private/var 的软链，run() 里 resolve() 过，比较前先对齐
            self.assertEqual(split.call_args.args[2], out.resolve())

    def test_partial_split_failure_returns_5(self):
        scenes = [scene.Scene(1, 0.0, 2.0), scene.Scene(2, 2.0, 4.0)]
        out = self.tmp / "切好的"
        with self._silence(), \
                mock.patch.object(scene.env, "probe_scenedetect",
                                  return_value=self._probe()), \
                mock.patch.object(scene, "ensure", return_value=True), \
                mock.patch.object(scene, "_detect", return_value=scenes), \
                mock.patch.object(scene, "_split_scenes", return_value=(1, 1)):
            self.assertEqual(scene.run(self._args(split=True, output_dir=str(out))), 5)

    def test_dry_run_stops_before_detection_result(self):
        """dry-run 只回显命令：检测没真跑，拿不到切点，不该继续往下算。"""
        with self._silence(), \
                mock.patch.object(scene.env, "probe_scenedetect",
                                  return_value=self._probe()), \
                mock.patch.object(scene, "ensure", return_value=True), \
                mock.patch.object(scene, "_detect", return_value=[]) as detect:
            self.assertEqual(scene.run(self._args(dry_run=True)), 0)
            self.assertTrue(detect.call_args.args[-1])  # dry_run 传下去了


class TestSplitScenes(unittest.TestCase):
    """逐段切割的循环。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.video = self.tmp / "素材.mp4"
        self.video.write_bytes(b"\x00" * 64)

    def tearDown(self):
        self._tmp.cleanup()

    def _ok_result(self):
        return mock.Mock(ok=True, returncode=0)

    def test_every_scene_gets_its_own_file(self):
        scenes = [scene.Scene(1, 0.0, 2.0), scene.Scene(2, 2.0, 4.0)]
        with mock.patch.object(scene.media, "find_tool", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(scene.runner_mod, "run",
                                  return_value=self._ok_result()) as run, \
                mock.patch.object(ui, "ok"), mock.patch.object(ui, "error"), \
                mock.patch.object(ui, "progress"):
            succeeded, failed = scene._split_scenes(
                self.video, scenes, self.tmp, copy_mode=False, dry_run=False)

        self.assertEqual((succeeded, failed), (2, 0))
        self.assertEqual(run.call_count, 2)
        targets = [Path(call.args[0][-1]).name for call in run.call_args_list]
        self.assertEqual(targets, ["素材_clip_001.mp4", "素材_clip_002.mp4"])

    def test_clip_number_comes_from_scene_not_loop_index(self):
        """场景序号才是文件名依据 —— 中间漏一个也不该整体错位。"""
        scenes = [scene.Scene(3, 0.0, 1.0), scene.Scene(7, 1.0, 2.0)]
        with mock.patch.object(scene.media, "find_tool", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(scene.runner_mod, "run",
                                  return_value=self._ok_result()) as run, \
                mock.patch.object(ui, "ok"), mock.patch.object(ui, "error"), \
                mock.patch.object(ui, "progress"):
            scene._split_scenes(self.video, scenes, self.tmp, copy_mode=False, dry_run=False)

        targets = [Path(call.args[0][-1]).name for call in run.call_args_list]
        self.assertEqual(targets, ["素材_clip_003.mp4", "素材_clip_007.mp4"])

    def test_failures_are_counted_not_raised(self):
        """一个片段切失败不该让整批停下 —— 剩下的还能用。"""
        scenes = [scene.Scene(1, 0.0, 1.0), scene.Scene(2, 1.0, 2.0), scene.Scene(3, 2.0, 3.0)]
        results = [
            mock.Mock(ok=True, returncode=0),
            mock.Mock(ok=False, returncode=1),
            mock.Mock(ok=True, returncode=0),
        ]
        with mock.patch.object(scene.media, "find_tool", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(scene.runner_mod, "run", side_effect=results), \
                mock.patch.object(ui, "ok"), mock.patch.object(ui, "error") as error, \
                mock.patch.object(ui, "progress"):
            succeeded, failed = scene._split_scenes(
                self.video, scenes, self.tmp, copy_mode=False, dry_run=False)

        self.assertEqual((succeeded, failed), (2, 1))
        self.assertTrue(error.called)

    def test_missing_ffmpeg_reports_all_as_failed(self):
        scenes = [scene.Scene(1, 0.0, 1.0), scene.Scene(2, 1.0, 2.0)]
        with mock.patch.object(scene.media, "find_tool", return_value=None), \
                mock.patch.object(scene.runner_mod, "run") as run, \
                mock.patch.object(ui, "error"), mock.patch.object(ui, "hint"), \
                mock.patch.object(ui, "progress"):
            succeeded, failed = scene._split_scenes(
                self.video, scenes, self.tmp, copy_mode=False, dry_run=False)

        self.assertEqual((succeeded, failed), (0, 2))
        run.assert_not_called()  # 没有 ffmpeg 就不该去试

    def test_batch_calls_are_quiet(self):
        """几十个片段的命令行长得一样，逐条回显会把结果冲掉。"""
        scenes = [scene.Scene(1, 0.0, 1.0), scene.Scene(2, 1.0, 2.0)]
        with mock.patch.object(scene.media, "find_tool", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(scene.runner_mod, "run",
                                  return_value=self._ok_result()) as run, \
                mock.patch.object(ui, "ok"), mock.patch.object(ui, "error"), \
                mock.patch.object(ui, "progress"):
            scene._split_scenes(self.video, scenes, self.tmp, copy_mode=False, dry_run=False)

        for call in run.call_args_list:
            self.assertTrue(call.kwargs.get("quiet"))


class TestProgressMarkers(unittest.TestCase):
    """给工作台看的进度标记。

    content-creation-workbench 在另一头实时解析这几行，把「第几条视频切到第几个片段」
    展示给用户（backend/app/services/scene_runner.py）。格式是两边的契约，
    所以在这里钉死：前缀、字段顺序、每段切完都要报一次。

    检测阶段只报「开始了」，给不出百分比 —— 要跑多少帧得整条过完才知道。
    测试盯着这一点：别哪天有人顺手编一个假的分母出来。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.video = self.tmp / "素材.mp4"
        self.video.write_bytes(b"\x00" * 64)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _markers(output: str) -> list[tuple[str, int, int]]:
        """从输出里挑出进度标记，返回 (阶段, 已完成, 总数) 列表。"""
        found = []
        for line in output.splitlines():
            if not line.startswith(ui.PROGRESS_PREFIX):
                continue
            _, phase, done, total = line.split()
            found.append((phase, int(done), int(total)))
        return found

    def test_marker_line_format(self):
        """一行四段：前缀、阶段、已完成、总数，用空格分隔。"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ui.progress("split", 3, 38)
        self.assertEqual(buffer.getvalue(), "#vct-progress split 3 38\n")

    def test_phase_constants(self):
        """阶段名是给机器看的，别改成中文。"""
        self.assertEqual(ui.PHASE_DETECT, "detect")
        self.assertEqual(ui.PHASE_SPLIT, "split")

    def test_every_clip_reports_progress(self):
        """切完一段报一次 —— 只在首尾报数的话，几十段切几分钟等于没进度。"""
        scenes = [scene.Scene(i, float(i), float(i + 1)) for i in range(1, 4)]
        buffer = io.StringIO()
        with mock.patch.object(scene.media, "find_tool", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(scene.runner_mod, "run",
                                  return_value=mock.Mock(ok=True, returncode=0)), \
                mock.patch.object(ui, "ok"), mock.patch.object(ui, "error"), \
                contextlib.redirect_stdout(buffer):
            scene._split_scenes(self.video, scenes, self.tmp, copy_mode=False, dry_run=False)

        self.assertEqual(
            self._markers(buffer.getvalue()),
            [("split", 1, 3), ("split", 2, 3), ("split", 3, 3)],
        )

    def test_failed_clip_still_advances(self):
        """切失败的片段也要往前走 —— 用户关心的是「还剩多少」，不是「成了几个」。"""
        scenes = [scene.Scene(1, 0.0, 1.0), scene.Scene(2, 1.0, 2.0)]
        buffer = io.StringIO()
        with mock.patch.object(scene.media, "find_tool", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(scene.runner_mod, "run",
                                  side_effect=[mock.Mock(ok=False, returncode=1),
                                               mock.Mock(ok=True, returncode=0)]), \
                mock.patch.object(ui, "ok"), mock.patch.object(ui, "error"), \
                contextlib.redirect_stdout(buffer):
            scene._split_scenes(self.video, scenes, self.tmp, copy_mode=False, dry_run=False)

        self.assertEqual(
            self._markers(buffer.getvalue()),
            [("split", 1, 2), ("split", 2, 2)],
        )

    def test_run_reports_detect_then_split_total(self):
        """整条流程：先报检测开始，检测完报出分母，最后回到 100%。"""
        scenes = [scene.Scene(1, 0.0, 2.0), scene.Scene(2, 2.0, 4.0)]
        args = SimpleNamespace(
            input=str(self.video), split=True, output_dir=str(self.tmp / "切好的"),
            detector="adaptive", threshold=None, min_len=0.6,
            copy=False, csv=False, dry_run=False,
        )
        probe = mock.Mock(ok=True, path=Path("/usr/local/bin/scenedetect"),
                          detail="", fix="", name="PySceneDetect")
        buffer = io.StringIO()
        with mock.patch.object(scene.env, "probe_scenedetect", return_value=probe), \
                mock.patch.object(scene, "ensure", return_value=True), \
                mock.patch.object(scene, "_detect", return_value=scenes), \
                mock.patch.object(scene.media, "find_tool", return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(scene.runner_mod, "run",
                                  return_value=mock.Mock(ok=True, returncode=0)), \
                mock.patch.object(ui, "header"), mock.patch.object(ui, "kv_table"), \
                mock.patch.object(ui, "step"), mock.patch.object(ui, "hint"), \
                mock.patch.object(ui, "ok"), mock.patch.object(ui, "blank"), \
                mock.patch.object(ui, "section"), mock.patch.object(ui, "info"), \
                contextlib.redirect_stdout(buffer):
            self.assertEqual(scene.run(args), 0)

        markers = self._markers(buffer.getvalue())
        self.assertEqual(markers[0], ("detect", 0, 1))
        self.assertEqual(markers[1], ("split", 0, 2))
        self.assertEqual(markers[-1], ("split", 2, 2))

    def test_single_scene_never_claims_a_split_phase(self):
        """单镜头不切片段（切出来就是原片），也就不该报切割阶段。"""
        scenes = [scene.Scene(1, 0.0, 115.0)]
        args = SimpleNamespace(
            input=str(self.video), split=True, output_dir=str(self.tmp / "切好的"),
            detector="adaptive", threshold=None, min_len=0.6,
            copy=False, csv=False, dry_run=False,
        )
        probe = mock.Mock(ok=True, path=Path("/usr/local/bin/scenedetect"),
                          detail="", fix="", name="PySceneDetect")
        buffer = io.StringIO()
        with mock.patch.object(scene.env, "probe_scenedetect", return_value=probe), \
                mock.patch.object(scene, "ensure", return_value=True), \
                mock.patch.object(scene, "_detect", return_value=scenes), \
                mock.patch.object(ui, "header"), mock.patch.object(ui, "kv_table"), \
                mock.patch.object(ui, "step"), mock.patch.object(ui, "hint"), \
                mock.patch.object(ui, "ok"), mock.patch.object(ui, "blank"), \
                mock.patch.object(ui, "section"), mock.patch.object(ui, "info"), \
                mock.patch.object(ui, "warn"), \
                contextlib.redirect_stdout(buffer):
            self.assertEqual(scene.run(args), 0)

        phases = [phase for phase, _, _ in self._markers(buffer.getvalue())]
        self.assertEqual(phases, ["detect"])


class TestDetect(unittest.TestCase):
    """检测这一步：跑命令 + 读产出。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.video = self.tmp / "素材.mp4"
        self.video.write_bytes(b"\x00" * 64)
        self.work = self.tmp / "work"
        self.work.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    @contextlib.contextmanager
    def _fake_tmpdir(self):
        """把 tempfile 换成一个我们握得住句柄的目录，好往里面塞 CSV。"""
        with mock.patch.object(scene.tempfile, "TemporaryDirectory",
                               return_value=contextlib.nullcontext(str(self.work))):
            yield

    def test_reads_csv_written_by_scenedetect(self):
        write_scenes_csv(self.work / "素材-Scenes.csv",
                         [row(1, 0.0, 1.733), row(2, 1.733, 3.333)])
        with self._fake_tmpdir(), \
                mock.patch.object(scene.env, "probe_scenedetect",
                                  return_value=mock.Mock(ok=True, path=Path("/bin/scenedetect"))), \
                mock.patch.object(scene.runner_mod, "run",
                                  return_value=mock.Mock(ok=True, returncode=0)):
            scenes = scene._detect(self.video, "adaptive", None, 0.6, dry_run=False)

        self.assertEqual(len(scenes), 2)
        self.assertAlmostEqual(scenes[1].start, 1.733, places=3)

    def test_command_failure_returns_none(self):
        with self._fake_tmpdir(), \
                mock.patch.object(scene.env, "probe_scenedetect",
                                  return_value=mock.Mock(ok=True, path=Path("/bin/scenedetect"))), \
                mock.patch.object(scene.runner_mod, "run",
                                  return_value=mock.Mock(ok=False, returncode=1)), \
                mock.patch.object(ui, "error"):
            self.assertIsNone(scene._detect(self.video, "adaptive", None, 0.6, False))

    def test_no_csv_produced_returns_none(self):
        """命令退出码是 0，但没写出 CSV —— 必须当成失败，不能当成「没有切点」。"""
        with self._fake_tmpdir(), \
                mock.patch.object(scene.env, "probe_scenedetect",
                                  return_value=mock.Mock(ok=True, path=Path("/bin/scenedetect"))), \
                mock.patch.object(scene.runner_mod, "run",
                                  return_value=mock.Mock(ok=True, returncode=0)), \
                mock.patch.object(ui, "error"):
            self.assertIsNone(scene._detect(self.video, "adaptive", None, 0.6, False))

    def test_missing_scenedetect_returns_none(self):
        with mock.patch.object(scene.env, "probe_scenedetect",
                               return_value=mock.Mock(ok=False, path=None)):
            self.assertIsNone(scene._detect(self.video, "adaptive", None, 0.6, False))

    def test_dry_run_returns_empty_without_reading(self):
        with self._fake_tmpdir(), \
                mock.patch.object(scene.env, "probe_scenedetect",
                                  return_value=mock.Mock(ok=True, path=Path("/bin/scenedetect"))), \
                mock.patch.object(scene.runner_mod, "run",
                                  return_value=mock.Mock(ok=True, returncode=0)):
            self.assertEqual(
                scene._detect(self.video, "adaptive", None, 0.6, dry_run=True), [])

    def test_malformed_csv_returns_none(self):
        (self.work / "broken.csv").write_text("完全不对的表头\n1,2\n", encoding="utf-8")
        with self._fake_tmpdir(), \
                mock.patch.object(scene.env, "probe_scenedetect",
                                  return_value=mock.Mock(ok=True, path=Path("/bin/scenedetect"))), \
                mock.patch.object(scene.runner_mod, "run",
                                  return_value=mock.Mock(ok=True, returncode=0)), \
                mock.patch.object(ui, "error"):
            self.assertIsNone(scene._detect(self.video, "adaptive", None, 0.6, False))


class TestDetectors(unittest.TestCase):
    """检测器表要跟 add_arguments 的 choices 对上，否则 argparse 会拒掉合法值。"""

    def test_adaptive_is_default(self):
        parser = mock.MagicMock()
        scene.add_arguments(parser)
        by_name = {call.args[0]: call for call in parser.add_argument.call_args_list}
        self.assertEqual(by_name["--detector"].kwargs["default"], "adaptive")

    def test_every_detector_has_a_chinese_label(self):
        for key, value in scene.DETECTORS.items():
            self.assertIsInstance(value, tuple)
            self.assertEqual(len(value), 2)
            self.assertTrue(all(value), key)

    def test_copy_flag_is_opt_in(self):
        """重编码必须是默认 —— copy 会把片段起点吸附到关键帧上。"""
        parser = mock.MagicMock()
        scene.add_arguments(parser)
        by_name = {call.args[0]: call for call in parser.add_argument.call_args_list}
        self.assertIn("--copy", by_name)
        self.assertNotIn("--precise", by_name)
        self.assertNotIn("default", by_name["--copy"].kwargs)  # store_true 默认 False


if __name__ == "__main__":
    unittest.main(verbosity=2)
