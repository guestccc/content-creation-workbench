"""外部媒体工具（ffmpeg / ffprobe）的定位、探测与子进程管理。

这些函数原本长在 `scene_runner.py` 里。智能混剪同样要起 ffmpeg 子进程、
同样要抽缩略图、同样要把 `~/.local/bin` 前插 PATH，于是平移到本模块共用 ——
复制一份的话，「子进程要整组杀」「PATH 要补」这两条踩过坑的规则就会出现两个副本，
迟早只改一处。

`scene_runner` 仍然从本模块把这些名字原样导出，所以 `tests/test_scene_runner.py`
里那些 `from app.services.scene_runner import terminate_process_group` 不受影响。

子进程管理的四条硬规则（三条模块共用，第四条见 scene_runner 顶部）：
1. stdio 必须落文件，绝不 PIPE —— ffmpeg 持续输出，无人读取的管道缓冲写满后
   子进程会永久阻塞在 write 上（表现为「任务跑到一半不动了」）；
2. 必须 start_new_session=True 并整组 kill —— 被调工具自己会拉起子进程，
   只杀直接子进程会留下孤儿继续啃 CPU。Windows 上 Python 忽略
   start_new_session，等价的隔离用下面的 CHILD_CREATION_FLAGS；
3. 要读子进程输出的地方一律按字节捕获、自己解码（run_probe / decode_output），
   绝不给 subprocess 传 text=True —— 理由见 decode_output 的注释；
4. 找到同名可执行文件不等于它就是这个工具，得跑一次 `-version` 自证
   （verify_tool）—— 理由见 verify_tool 的注释。自证失败还要分清楚是
   「跑不起来」还是「自称是别的工具」，两者给用户的修复建议不同
   （见 PROBE_* 常量）。
"""

import errno
import locale
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Sequence, Tuple

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Windows 上 start_new_session 是空操作，子进程仍挂在后端控制台里 —— 终端
#: 里按 Ctrl+C 时，CTRL_C_EVENT 会广播给控制台的全部进程，正在跑的抓取 /
#: 混剪 / 分割 / 字幕任务会被连带打断（MC 打「Received interrupt signal 2」
#: 后退出，任务被标成失败）。CREATE_NEW_PROCESS_GROUP 把子进程放进不吃
#: 控制台 Ctrl+C 的新进程组，这才是 Windows 侧的等价隔离。POSIX 上为 0，
#: start_new_session=True 已经完成同样的事。要停任务走各页面的取消按钮
#: （terminate_process_group 整组杀，不受此影响）；直接关终端窗口仍会带走
#: 子进程（CTRL_CLOSE_EVENT 不看进程组），这是预期行为。
CHILD_CREATION_FLAGS = (
    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
)


def child_env() -> Dict[str, str]:
    """构造子进程环境变量：把几个「工具常在但 PATH 里没有」的目录前插到 PATH。

    背景：uvicorn 若从图形界面或 launchd 启动，PATH 会非常贫瘠，而工具
    只会去 PATH 里找。补上这几处：
    - `~/.local/bin`：vct 自己就在这里找 scenedetect；
    - `<工具箱>/ffmpeg-bin`：vct 没找到 ffmpeg 时的兜底位置；
    - `<VideoCaptioner>/../ffmpeg-bin`：同一层级的另一个 ffmpeg 存放点，
      字幕提取要先把视频转成音频，缺 ffmpeg 会全量失败。

    重复目录无害（PATH 里同名目录只有第一个生效），所以不做去重判断。
    """
    env = dict(os.environ)
    extra = [
        str(Path.home() / ".local" / "bin"),
        str(Path(settings.SCENE_VCT_PATH).resolve().parent / "ffmpeg-bin"),
    ]
    if settings.SUBTITLE_VC_ROOT:
        extra.append(str(Path(settings.SUBTITLE_VC_ROOT).expanduser().parent / "ffmpeg-bin"))

    current = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(extra + ([current] if current else []))
    return env


def find_tool(name: str) -> Optional[str]:
    """在 child_env() 的 PATH 里找可执行文件，找不到返回 None。

    注意：**只按文件名找**，找到的东西不一定是它自己 —— 判定「这个工具真的
    能用」要用 verify_tool。
    """
    return shutil.which(name, path=child_env().get("PATH"))


def decode_output(data: Optional[bytes]) -> str:
    """把子进程的输出字节解成文本：先 UTF-8，再本地代码页，都不行就替换坏字节。

    按字节捕获、自己解码，而不是给 subprocess 传 text=True。真实踩过的坑：
    中文 Windows 的系统代码页是 GBK，而 ffprobe 的 JSON 会**原样回显素材路径**
    （UTF-8），text=True 的解码在 subprocess 的读线程里抛 UnicodeDecodeError，
    result.stdout 变成 None —— 上层要么 TypeError 要么 AttributeError，人看到
    的是「明明能播的视频，却读不出规格」。探测看不懂输出顶多是返回 None，
    绝不该崩，所以宁可替换坏字节也不要抛。
    """
    if not data:
        return ""
    candidates = ["utf-8"]
    preferred = locale.getpreferredencoding(False)
    if preferred and preferred.lower() not in ("utf-8", "utf8"):
        candidates.append(preferred)
    for encoding in candidates:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


class ProbedOutput(NamedTuple):
    """一次只读探测的结果；stdout / stderr 已经解码，不会是 None。"""

    returncode: int
    stdout: str
    stderr: str


#: 探测失败的原因码。分成这几档是因为「不可用」有好几种成因，彼此要给的
#: 修复建议完全不同：架构不匹配要换版本，没权限要 chmod，冒充要换文件。
#: 笼统报成「找到的同名文件不是 ffmpeg」会把换版本的人往「谁改了我的文件名」
#: 上带（真实踩过：macOS 26 之后系统不再支持 Intel 版二进制，PATH 里那份
#: 是好的 ffmpeg，只是跑不起来，提示却说是别的工具改了名）。
PROBE_OK = ""
PROBE_MISSING = "missing"  # 压根没给路径
PROBE_BADARCH = "badarch"  # 架构与本机不符（EBADARCH，macOS 独有）
PROBE_EACCES = "eacces"  # 没有执行权限
PROBE_UNSTARTABLE = "unstartable"  # 其它起不来的原因（文件损坏等）
PROBE_TIMEOUT = "timeout"
PROBE_NONZERO = "nonzero"  # 跑起来了，但自己报错退出
PROBE_IMPOSTOR = "impostor"  # 正常退出，却自称是别的工具

#: 起不来时的修复建议，按原因码取。缺的键回落到 PROBE_UNSTARTABLE 那条。
_UNSTARTABLE_HINTS = {
    PROBE_BADARCH: (
        "文件架构与本机不符（Apple Silicon 需要 arm64 版，且 macOS 26 之后"
        "系统已不再提供 Rosetta）：换一份对应架构的版本，或从包管理器重新安装"
    ),
    PROBE_EACCES: "文件没有执行权限：chmod +x 之后重试",
    PROBE_UNSTARTABLE: "文件跑不起来：确认它是一份完整、可用的程序",
    PROBE_TIMEOUT: "文件响应超时：确认它不是卡住的脚本或需要交互的程序",
    PROBE_NONZERO: "文件自检时报错退出：确认它是一份完整、可用的程序",
}


class SpawnResult(NamedTuple):
    """一次探测的完整结果：跑没跑起来、失败是为什么。

    `run_probe` 只看 `output` 判成败，依赖自检还要看 `code` 决定给什么
    修复建议，所以原因一并带回来，不必重跑一次子进程。
    """

    output: Optional[ProbedOutput]
    code: str  # PROBE_* 之一，成功时是 PROBE_OK
    message: str  # 失败原因的人话说明，成功时是空串


def describe_os_error(exc: OSError) -> str:
    """把「进程起不来」的 OSError 说成人话。

    架构不匹配在 macOS 上抛的是 `[Errno 86] Bad CPU type in executable`，
    这是本机最可能撞上的那一种（系统停掉 Rosetta 之后，PATH 里所有 Intel 版
    二进制都会以这个错报出来），所以单独认出来，别退化成一句 "No such file"。
    """
    if exc.errno is not None and exc.errno == getattr(errno, "EBADARCH", None):
        return "系统不支持这个文件的架构（Bad CPU type in executable）"
    if exc.errno == errno.EACCES:
        return "没有执行权限"
    if exc.errno == errno.ENOENT:
        return "文件不存在"
    return exc.strerror or str(exc)


def spawn_probe(
    argv: Sequence[str],
    *,
    timeout: float = 30,
    cwd: Optional[str] = None,
) -> SpawnResult:
    """起一次只读探测子进程，连失败原因一起返回。

    与执行任务用的 subprocess.Popen 不同：探测命令输出很短、一定会退出，
    所以这里可以 capture_output（不必落文件）。但**不能传 text=True**，
    理由见 decode_output。
    """
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
            env=child_env(),
        )
    except subprocess.TimeoutExpired:
        logger.debug("探测命令超时 | %s", argv[0] if argv else "")
        return SpawnResult(None, PROBE_TIMEOUT, "")
    except OSError as exc:
        logger.debug("探测命令起不来 | %s | %s", argv[0] if argv else "", exc)
        # 逐个比对而不是拿 errno 当字典键：EBADARCH 只在 macOS 上存在，
        # 非 macOS 取出的是 None，而 OSError 的 errno 本身也可能是 None，
        # 拿 None 去查表会把「没有 errno 的错误」错判成架构不匹配。
        badarch = getattr(errno, "EBADARCH", None)
        if exc.errno is not None and exc.errno == badarch:
            code = PROBE_BADARCH
        elif exc.errno == errno.EACCES:
            code = PROBE_EACCES
        else:
            code = PROBE_UNSTARTABLE
        return SpawnResult(None, code, describe_os_error(exc))
    return SpawnResult(
        ProbedOutput(
            returncode=result.returncode,
            stdout=decode_output(result.stdout),
            stderr=decode_output(result.stderr),
        ),
        PROBE_OK,
        "",
    )


def run_probe(
    argv: Sequence[str],
    *,
    timeout: float = 30,
    cwd: Optional[str] = None,
) -> Optional[ProbedOutput]:
    """跑一次只读探测命令（ffprobe 探测 / `-version` 自检），失败返回 None。

    进程起不来或超时返回 None，调用方据此降级 —— 只要成败、不要原因的地方
    用这个；要按失败原因分岔（依赖自检）用 spawn_probe。
    """
    return spawn_probe(argv, timeout=timeout, cwd=cwd).output


def verify_tool_detail(name: str, path: Optional[str]) -> Tuple[str, str, str]:
    """自检一个工具，返回 `(版本号, 失败码, 失败说明)`；版本号非空即为通过。

    只在 PATH 里找到同名文件是不够的。真实踩过：`~/.local/bin/ffprobe.exe`
    其实是一个 ffmpeg.exe 的副本（两者 md5 一模一样），名字探测一路通过，
    自检也报「就绪」，直到任务跑起来探测素材规格才炸，报的还是「素材读不出来」。
    这里靠 `-version` 首行的自报名号判定：真 ffprobe 说 `ffprobe version 6.1.1`，
    冒充者说的是自己原本的名字。

    失败码是给「怎么修」用的，所以拆得比「能/不能」细：跑不起来（架构不匹配、
    没权限）和跑得起来却自称是别的工具，修复动作完全不同 —— 前者换版本，
    后者换文件。调用方只要成败的话用 verify_tool。
    """
    if not path:
        return "", PROBE_MISSING, ""
    spawned = spawn_probe([path, "-version"], timeout=10)
    if spawned.output is None:
        logger.warning(
            "工具自检失败（跑不起来）| %s | %s | %s | %s",
            name,
            path,
            spawned.code,
            spawned.message,
        )
        return "", spawned.code, spawned.message
    result = spawned.output
    if result.returncode != 0:
        logger.warning(
            "工具自检失败（返回码 %s）| %s | %s", result.returncode, name, path
        )
        return "", PROBE_NONZERO, ""
    # 首行形如 `<name> version <版本> ...`；stdout 空时退回 stderr（个别版本走 stderr）
    text = result.stdout.strip() or result.stderr.strip()
    head = text.split()
    if len(head) < 2 or head[0].lower() != name.lower() or head[1].lower() != "version":
        actual = " ".join(head[:2]) or "（无输出）"
        logger.warning(
            "工具自检失败（自称是别的工具）| 期望=%s | 实际=%s | %s",
            name,
            actual,
            path,
        )
        return "", PROBE_IMPOSTOR, actual
    return (head[2] if len(head) > 2 else "unknown"), PROBE_OK, ""


def verify_tool(name: str, path: Optional[str]) -> str:
    """跑一次 `<tool> -version`，确认这个文件真的是它自己，返回版本号。

    跑不起来 / 超时 / 返回码非 0 / 名号对不上，一律返回空串（调用方据此
    当作「不可用」）。要看是哪一种失败，用 verify_tool_detail。
    """
    return verify_tool_detail(name, path)[0]


def dependency_status(
    name: str,
    *,
    purpose: str,
    missing_detail: str,
    missing_hint: str,
    impostor_detail: str = "",
    impostor_hint: str = "",
) -> dict:
    """一项外部依赖的自检结果（场景分割 / 智能混剪的 dependencies[] 同一份契约）。

    字段就是 `DependencyStatus` / `MixDependencyStatus` 那五个：name / ok /
    path / detail / fix_hint。找到同名文件还不够，要 `-version` 自证是它自己，
    否则页面会显示成「就绪」，用户却在任务里撞上「读不出素材规格」。

    自检失败分两种，说明与建议都不一样，所以分开处理：
    - **跑不起来**（架构不匹配 / 没权限 / 文件损坏）：名字没错，是这份文件在
      本机执行不了。修复建议按 errno 给（见 _UNSTARTABLE_HINTS）。
    - **自称是别的工具**：文件能跑，只是它根本不是这个工具。这时才用得上
      调用方传的 impostor_detail。

    早先这两种都归到 impostor 一档，于是 macOS 停掉 Rosetta 之后，PATH 里那份
    好好的 ffmpeg 被报成「可能是别的工具的副本被改名」，把用户往查文件名的
    方向带，而真正该做的是换一份 arm64 版本。

    Args:
        purpose: 可用时的说明（页面只在不可用时展示 detail，这条是给自检就绪
            的人看的）。
        missing_detail / missing_hint: PATH 里没有这个工具时的说明与修复建议。
        impostor_detail / impostor_hint: 找到了、跑得起来，但名号对不上时的
            说明与修复建议；不给就回落到 missing 那两条（对任务的影响一样：
            都用不了）。
    """
    path = find_tool(name)
    if not path:
        return {
            "name": name,
            "ok": False,
            "path": "",
            "detail": missing_detail,
            "fix_hint": missing_hint,
        }

    version, failure_code, failure_message = verify_tool_detail(name, path)
    if version:
        return {
            "name": name,
            "ok": True,
            "path": path,
            "detail": purpose,
            "fix_hint": "",
        }

    if failure_code == PROBE_IMPOSTOR:
        logger.warning("依赖自检：找到的 %s 不是它自己 | %s", name, path)
        return {
            "name": name,
            "ok": False,
            "path": path,
            "detail": impostor_detail or missing_detail,
            "fix_hint": impostor_hint or missing_hint,
        }

    logger.warning(
        "依赖自检：%s 跑不起来 | %s | %s | %s",
        name,
        path,
        failure_code,
        failure_message,
    )
    detail = "找到的文件无法执行"
    if failure_message:
        detail = f"{detail}：{failure_message}"
    return {
        "name": name,
        "ok": False,
        "path": path,
        "detail": detail,
        "fix_hint": _UNSTARTABLE_HINTS.get(
            failure_code, _UNSTARTABLE_HINTS[PROBE_UNSTARTABLE]
        ),
    }


def terminate_process_group(pid: int, grace: float) -> None:
    """终止一个子进程组：先 SIGTERM，最多等 grace 秒，再 SIGKILL。

    所有平台差异都收在这一个函数里，不散落。任何异常都吞掉 —— 清理动作
    不能因为「进程已经自己退了」这种好事而炸掉主流程。

    Args:
        pid: 子进程 PID（start_new_session=True 后它同时也是进程组 ID，
            所以直接 killpg(pid)，不需要先 getpgid 多一次竞态窗口）。
        grace: 等进程组自行退出的宽限秒数。
    """
    if pid <= 0:
        return
    try:
        if os.name == "nt":
            # Windows 没有进程组信号，用 taskkill 整棵树杀掉
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=grace + 5,
                check=False,
            )
            return

        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                # kill 0 只探活不发信号；ProcessLookupError 说明已经退干净
                os.killpg(pid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.1)
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        logger.debug("终止进程组时进程已退出或不可达 | pid=%s | %s", pid, exc)
    except Exception:  # noqa: BLE001 - 清理路径兜底，绝不向上抛
        logger.exception("终止进程组出现异常 | pid=%s", pid)


def command_line(pid: int) -> str:
    """取一个 PID 的命令行，取不到返回空串。

    用于孤儿回收前的身份核对：机器重启后 PID 会被复用，盲杀可能干掉
    无辜进程（比如用户刚打开的编辑器）。
    """
    if pid <= 0 or os.name == "nt":
        return ""
    result = run_probe(["ps", "-o", "command=", "-p", str(pid)], timeout=5)
    return result.stdout.strip() if result is not None else ""


def read_log_tail(log_path: Path, limit: int = 2000) -> str:
    """读子进程日志尾部，作为失败原因展示给用户（被调工具的真实报错）。"""
    try:
        data = log_path.read_bytes()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return text[-limit:].strip()


def probe_duration(video: Path) -> Optional[float]:
    """用 ffprobe 探测视频时长（秒），失败返回 None（不阻断主流程）。"""
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return None
    result = run_probe(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video),
        ]
    )
    if result is None:
        return None
    try:
        return round(float(result.stdout.strip()), 3)
    except ValueError:
        return None


def probe_dimensions(video: Path) -> Optional[Tuple[int, int]]:
    """用 ffprobe 探测视频宽高（像素），失败返回 None（不阻断主流程）。

    前端片段卡片按它算画幅比例（横屏 / 竖屏 / 方形），探测不到就让前端
    退回默认比例，卡片照常渲染。
    """
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return None
    result = run_probe(
        [
            ffprobe, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=x",
            str(video),
        ]
    )
    if result is None:
        return None
    try:
        width_text, _, height_text = result.stdout.strip().partition("x")
        width, height = int(width_text), int(height_text)
        return (width, height) if width > 0 and height > 0 else None
    except ValueError:
        return None


def generate_thumbnail(video_path: Path, thumb_path: Path) -> bool:
    """给片段抽首帧生成缩略图（首次请求缩略图接口时调用，落盘缓存）。"""
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return False
    try:
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                ffmpeg, "-y", "-ss", "0.1", "-i", str(video_path),
                "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4",
                str(thumb_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
            env=child_env(),
        )
        return result.returncode == 0 and thumb_path.is_file()
    except (OSError, subprocess.TimeoutExpired):
        return False
