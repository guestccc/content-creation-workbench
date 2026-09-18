"""测试替身：模拟 vct 子进程的 FakePopen。

SceneRunner 通过 popen_factory 注入它，测试在主线程同步跑完整个执行流程，
不起线程、不起真子进程、不需要真视频，整个执行层测试套件应在 1 秒内跑完。

行为契约（与真实 vct 对齐）：
- 首次 poll() 时在 -o 指定的输出目录里「生产」产物（CSV + 片段文件），
  然后返回脚本设定的退出码；
- 产物内容与退出码由每个用例通过 script 字典控制；
- 每 poll() 一次就往 stdout 句柄写一行 script["log_lines"]，模拟真实 vct
  边跑边往 vct.log 里写进度标记 —— 被测代码正是靠读这个文件拿进度的。
"""

import json
from pathlib import Path
from typing import List, Optional

# vct 切点 CSV 的表头（与 vctl/commands/scene.py 的真实产物一致）
CSV_HEADER = "序号,开始(秒),结束(秒),时长(秒),开始时间码,结束时间码"


def _make_csv(scenes: int) -> str:
    """生成一份有 scenes 个镜头的 CSV 内容。"""
    lines = [CSV_HEADER]
    cursor = 0.0
    for index in range(1, scenes + 1):
        duration = 2.0 + index * 0.5
        end = cursor + duration
        lines.append(f"{index},{cursor:.3f},{end:.3f},{duration:.3f},00:00:00,00:00:00")
        cursor = end
    return "\n".join(lines) + "\n"


class FakePopen:
    """模拟 subprocess.Popen 的最小接口：pid / poll / argv 记录。"""

    #: 记录所有实例，便于用例断言「起了几次进程、参数是什么」
    instances: List["FakePopen"] = []

    @classmethod
    def reset(cls) -> None:
        """清空实例记录，每个用例开始前调用。"""
        cls.instances = []

    def __init__(
        self,
        argv,
        stdout=None,
        stderr=None,
        stdin=None,
        start_new_session: bool = False,
        creationflags: int = 0,
        cwd: Optional[str] = None,
        env=None,
        script: Optional[dict] = None,
    ) -> None:
        """按 script 配置构造一个假进程。

        Args:
            argv: 完整命令行（据此解析输出目录、输入视频、是否 --split）。
            script: 行为脚本，键：
                exit_code          退出码，默认 0；
                scenes             CSV 里的镜头数，默认 3；
                clips              生成的片段数，默认等于 scenes（split 且非单镜头时）；
                hang               True 时 poll 永远返回 None（模拟卡死的进程）；
                polls_before_exit  退出前先空转多少次 poll，默认 0（首次 poll 即退出）。
                                   设成正数才能观察到「跑到一半」的中间状态；
                log_lines          每次 poll 往 stdout 写一行，模拟 vct 的进度标记；
                on_poll            每次 poll 时调用的回调 fn(proc, 第几次)，
                                   测试用它充当「前端轮询接口」读当时的进度。
        """
        self.argv = list(argv)
        self.cwd = cwd
        self.env = env
        self.start_new_session = start_new_session
        self.creationflags = creationflags
        self.script = dict(script or {})
        self.pid = 40000 + len(FakePopen.instances)
        # 保存 stdout 句柄：真实 vct 的 stdout 被重定向到 vct.log，
        # 被测代码正是读这个文件来解析进度的，替身得往同一个地方写。
        self.stdout = stdout
        self.poll_count = 0
        self._returncode: Optional[int] = None
        self._produced = False
        FakePopen.instances.append(self)

    # ------------------------------------------------------------------
    # 被测代码用到的接口
    # ------------------------------------------------------------------

    def poll(self) -> Optional[int]:
        """首次调用生产产物并返回退出码；hang 模式下永远返回 None。

        polls_before_exit 次空转是为了复现真实的时间轴：vct 跑几十秒到几分钟，
        进度字段是在这期间被一次次刷新的，只有「退出前先 poll 几次」才测得到
        中间状态，而不是只看到首尾两个点。
        """
        self.poll_count += 1
        probe = self.script.get("on_poll")
        if probe is not None:
            probe(self, self.poll_count)

        if self.script.get("hang"):
            self._emit_log_line()
            return None

        remaining = int(self.script.get("polls_before_exit", 0))
        if self.poll_count <= remaining:
            self._emit_log_line()
            return None

        self._emit_log_line()
        if not self._produced:
            self._produce()
            self._produced = True
            self._returncode = int(self.script.get("exit_code", 0))
        return self._returncode

    def _emit_log_line(self) -> None:
        """把下一条预置日志写进 stdout 句柄（写完即 flush，与 vct 一致）。"""
        lines: List[str] = list(self.script.get("log_lines") or [])
        index = self.poll_count - 1
        if index >= len(lines) or self.stdout is None:
            return
        try:
            self.stdout.write((lines[index] + "\n").encode("utf-8"))
            self.stdout.flush()
        except (OSError, ValueError, AttributeError):
            # 句柄已被被测代码关掉（进程退出后），这在真实场景里也不该崩
            pass

    # ------------------------------------------------------------------
    # 产物模拟
    # ------------------------------------------------------------------

    def _produce(self) -> None:
        """按脚本在输出目录里写 CSV 与片段文件。"""
        out_dir = self._arg_after("-o") or self.cwd
        if out_dir is None:
            return
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)

        video = Path(self.argv[2]) if len(self.argv) > 2 else Path("video.mp4")
        stem = video.stem
        split = "--split" in self.argv

        scenes = int(self.script.get("scenes", 3))
        (out / f"{stem}_scenes.csv").write_text(_make_csv(scenes), encoding="utf-8")

        # 单镜头（scenes<=1）时 vct 不会切任何片段；clips 可被脚本显式覆盖
        if split and scenes > 1:
            clips = int(self.script.get("clips", scenes))
            for index in range(1, clips + 1):
                (out / f"{stem}_clip_{index:03d}.mp4").write_bytes(b"fake")

    def _arg_after(self, flag: str) -> Optional[str]:
        """取 argv 中某个选项后面的值。"""
        try:
            return self.argv[self.argv.index(flag) + 1]
        except (ValueError, IndexError):
            return None


# ---------------------------------------------------------------------------
# 视频字幕提取（VideoCaptioner）
# ---------------------------------------------------------------------------


# 一份最小可用的 .srt（两条字幕），替身默认往输出目录写这个内容
DEFAULT_SRT = (
    "1\n"
    "00:00:00,000 --> 00:00:02,000\n"
    "第一句字幕\n"
    "\n"
    "2\n"
    "00:00:02,000 --> 00:00:04,000\n"
    "第二句字幕\n"
    "\n"
)


class FakeVcPopen:
    """模拟 VideoCaptioner transcribe 子进程的最小接口。

    argv 形状（与 subtitle_runner.build_argv 对齐）：
        [launcher..., "transcribe", <video>, [--asr X] [--language Y]
         --format srt, -o, <输出文件完整路径>, --quiet]
    与 FakePopen 的关键区别：launcher 前缀长度不定，视频参数要靠
    "transcribe" 定位；-o 的值是分配好的输出文件（真实 VC 对带扩展名的
    路径原样写盘），替身也直接写这个路径。

    script 键（缺省即一条顺利转写：退出码 0 + 产出一份两条字幕的 .srt）：
        exit_code          退出码，默认 0；
        no_output          True 时不产出 .srt（退出码 0 但产物缺失的场景）；
        srt_content        自定义 .srt 内容，默认 DEFAULT_SRT；
        empty_output       True 时产出一个 0 字节 .srt（判失败用）；
        hang               True 时 poll 永远返回 None；
        polls_before_exit  退出前先空转多少次 poll；
        log_lines          每次 poll 往 stdout 写一行；
        on_poll            每次 poll 时调用的回调 fn(proc, 第几次)。
    """

    #: 记录所有实例，便于用例断言「起了几次进程、参数是什么」
    instances: List["FakeVcPopen"] = []

    @classmethod
    def reset(cls) -> None:
        """清空实例记录，每个用例开始前调用。"""
        cls.instances = []

    def __init__(
        self,
        argv,
        stdout=None,
        stderr=None,
        stdin=None,
        start_new_session: bool = False,
        creationflags: int = 0,
        cwd: Optional[str] = None,
        env=None,
        script: Optional[dict] = None,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.env = env
        self.start_new_session = start_new_session
        self.creationflags = creationflags
        self.script = dict(script or {})
        self.pid = 50000 + len(FakeVcPopen.instances)
        self.stdout = stdout
        self.poll_count = 0
        self._returncode: Optional[int] = None
        self._produced = False
        FakeVcPopen.instances.append(self)

    def poll(self) -> Optional[int]:
        """首次（空转次数用尽后的）调用生产产物并返回退出码；hang 时永远 None。"""
        self.poll_count += 1
        probe = self.script.get("on_poll")
        if probe is not None:
            probe(self, self.poll_count)

        if self.script.get("hang"):
            self._emit_log_line()
            return None

        remaining = int(self.script.get("polls_before_exit", 0))
        if self.poll_count <= remaining:
            self._emit_log_line()
            return None

        self._emit_log_line()
        if not self._produced:
            self._produce()
            self._produced = True
            self._returncode = int(self.script.get("exit_code", 0))
        return self._returncode

    def _emit_log_line(self) -> None:
        """把下一条预置日志写进 stdout 句柄（与真实 vc 的日志文件是同一个地方）。"""
        lines: List[str] = list(self.script.get("log_lines") or [])
        index = self.poll_count - 1
        if index >= len(lines) or self.stdout is None:
            return
        try:
            self.stdout.write((lines[index] + "\n").encode("utf-8"))
            self.stdout.flush()
        except (OSError, ValueError, AttributeError):
            # 句柄已被被测代码关掉（进程退出后），这在真实场景里也不该崩
            pass

    def _produce(self) -> None:
        """按脚本往 -o 指定的输出文件写 .srt（除非 no_output）。

        真实 VC 在 -o 是带扩展名的文件路径时原样写这个路径，替身照做；
        日志同时打印结果路径（--quiet 契约），方便用例复现「日志只有一行
        路径」的真实形态。
        """
        output = self._arg_after("-o")
        if output is None:
            return
        out = Path(output)

        if not self.script.get("no_output"):
            out.parent.mkdir(parents=True, exist_ok=True)
            content = b"" if self.script.get("empty_output") else self.script.get(
                "srt_content", DEFAULT_SRT
            )
            if isinstance(content, str):
                content = content.encode("utf-8")
            out.write_bytes(content)

        # --quiet 下真实 VC 退出前打印一行输出路径（无论产物是否写成功，只要
        # 走到 save 这一步就打印）—— 替身在非 no_output 时照做
        if self.stdout is not None and not self.script.get("no_output"):
            try:
                self.stdout.write((str(out) + "\n").encode("utf-8"))
                self.stdout.flush()
            except (OSError, ValueError, AttributeError):
                pass

    def _arg_after(self, flag: str) -> Optional[str]:
        """取 argv 中某个选项后面的值。"""
        try:
            return self.argv[self.argv.index(flag) + 1]
        except (ValueError, IndexError):
            return None


# ---------------------------------------------------------------------------
# 素材抓取（MediaCrawler）
# ---------------------------------------------------------------------------

def mc_note(note_id: str, **overrides) -> dict:
    """一条 xhs 形态的原始 jsonl 行（其它平台用例用 overrides 换掉字段名）。"""
    note = {
        "note_id": note_id,
        "type": "note",
        "title": f"标题-{note_id}",
        "desc": f"正文-{note_id}",
        "nickname": "测试博主",
        "liked_count": 10,
        "comment_count": 2,
        "share_count": 1,
        "time": 1747000000000,  # 毫秒时间戳
        "note_url": f"https://www.xiaohongshu.com/explore/{note_id}",
        "image_list": "https://img.example/1.webp,https://img.example/2.webp",
        "source_keyword": "保温杯",
    }
    note.update(overrides)
    return note


DEFAULT_MC_NOTES = [mc_note("note1"), mc_note("note2"), mc_note("note3")]


class FakeMcPopen:
    """模拟 MediaCrawler 子进程（python main.py ...）的最小接口。

    argv 形状（与 crawl_runner.build_argv 对齐）：
        [launcher..., "main.py", "--platform", p, "--type", t, ...,
         "--save_data_path", <dir>, ...]
    产物落盘位置（与 MC 的 AsyncFileWriter._get_file_path 约定一致）：
        {--save_data_path 的值}/{--platform 的值}/jsonl/{--type 的值}_contents_fake.jsonl
    （注意目录名是 jsonl 不是 json —— 文件类型名就是目录名。）

    script 键（缺省即一次顺利抓取：退出码 0 + 三条 xhs 笔记）：
        exit_code          退出码，默认 0；
        notes              落盘的原始 jsonl 行列表，默认 DEFAULT_MC_NOTES；
                           换平台时给对应平台形态的行即可（字段名按 FIELD_MAP）；
        no_output          True 时完全不写 jsonl（风控拦截、一条没抓到的场景）；
        notes_per_poll     每次 poll 先落几条（默认 0：全部笔记在退出那次 poll
                           一起写）。设成正数 + polls_before_exit 才观察得到
                           「进度走到一半」的中间状态；
        hang               True 时 poll 永远返回 None（hang 前已写的不受影响）；
        polls_before_exit  退出前先空转多少次 poll，默认 0；
        log_lines          每次 poll 往 stdout 写一行（mc.log）；
        on_poll            每次 poll 时调用的回调 fn(proc, 第几次)。
    """

    #: 记录所有实例，便于用例断言「起了几次进程、参数是什么」
    instances: List["FakeMcPopen"] = []

    @classmethod
    def reset(cls) -> None:
        """清空实例记录，每个用例开始前调用。"""
        cls.instances = []

    def __init__(
        self,
        argv,
        stdout=None,
        stderr=None,
        stdin=None,
        start_new_session: bool = False,
        creationflags: int = 0,
        cwd: Optional[str] = None,
        env=None,
        script: Optional[dict] = None,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.env = env
        self.start_new_session = start_new_session
        self.creationflags = creationflags
        self.script = dict(script or {})
        self.pid = 70000 + len(FakeMcPopen.instances)
        self.stdout = stdout
        self.poll_count = 0
        self._returncode: Optional[int] = None
        self._written = 0
        FakeMcPopen.instances.append(self)

    def poll(self) -> Optional[int]:
        """每次 poll 按需落几条笔记；空转次数用尽后补齐全部并返回退出码。"""
        self.poll_count += 1
        probe = self.script.get("on_poll")
        if probe is not None:
            probe(self, self.poll_count)

        # 逐 poll 落盘不受 hang 影响：取消/超时场景恰恰要「卡住但已抓到一半」
        per_poll = int(self.script.get("notes_per_poll", 0))
        if per_poll:
            self._write_notes(per_poll)

        if self.script.get("hang"):
            self._emit_log_line()
            return None

        remaining = int(self.script.get("polls_before_exit", 0))
        if self.poll_count <= remaining:
            self._emit_log_line()
            return None

        self._emit_log_line()
        self._write_notes()  # 退出前把剩下的笔记补齐（取消/超时场景也要有产物）
        self._returncode = int(self.script.get("exit_code", 0))
        return self._returncode

    def _emit_log_line(self) -> None:
        """把下一条预置日志写进 stdout 句柄（与 mc.log 是同一个地方）。"""
        lines: List[str] = list(self.script.get("log_lines") or [])
        index = self.poll_count - 1
        if index >= len(lines) or self.stdout is None:
            return
        try:
            self.stdout.write((lines[index] + "\n").encode("utf-8"))
            self.stdout.flush()
        except (OSError, ValueError, AttributeError):
            # 句柄已被被测代码关掉（进程退出后），这在真实场景里也不该崩
            pass

    def _write_notes(self, count: Optional[int] = None) -> None:
        """把 script["notes"] 里尚未落盘的行写进任务的 jsonl（append 语义）。"""
        if self.script.get("no_output"):
            return
        save_path = self._arg_after("--save_data_path")
        platform = self._arg_after("--platform")
        crawler_type = self._arg_after("--type")
        if not (save_path and platform and crawler_type):
            return

        notes: List[dict] = list(self.script.get("notes", DEFAULT_MC_NOTES))
        pending = notes[self._written:]
        if not pending:
            return
        if count is not None:
            pending = pending[:count]

        target = Path(save_path) / platform / "jsonl"
        target.mkdir(parents=True, exist_ok=True)
        with (target / f"{crawler_type}_contents_fake.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            for note in pending:
                handle.write(json.dumps(note, ensure_ascii=False) + "\n")
        self._written += len(pending)

    def _arg_after(self, flag: str) -> Optional[str]:
        """取 argv 中某个选项后面的值。"""
        try:
            return self.argv[self.argv.index(flag) + 1]
        except (ValueError, IndexError):
            return None
