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
