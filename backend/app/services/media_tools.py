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
   （verify_tool）—— 理由见 verify_tool 的注释。
"""

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


def run_probe(
    argv: Sequence[str],
    *,
    timeout: float = 30,
    cwd: Optional[str] = None,
) -> Optional[ProbedOutput]:
    """跑一次只读探测命令（ffprobe 探测 / `-version` 自检），失败返回 None。

    与执行任务用的 subprocess.Popen 不同：探测命令输出很短、一定会退出，
    所以这里可以 capture_output（不必落文件）。但**不能传 text=True**，
    理由见 decode_output。进程起不来或超时返回 None（调用方据此降级）。
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("探测命令执行失败 | %s | %s", argv[0] if argv else "", exc)
        return None
    return ProbedOutput(
        returncode=result.returncode,
        stdout=decode_output(result.stdout),
        stderr=decode_output(result.stderr),
    )


def verify_tool(name: str, path: Optional[str]) -> str:
    """跑一次 `<tool> -version`，确认这个文件真的是它自己，返回版本号。

    只在 PATH 里找到同名文件是不够的。真实踩过：`~/.local/bin/ffprobe.exe`
    其实是一个 ffmpeg.exe 的副本（两者 md5 一模一样），名字探测一路通过，
    自检也报「就绪」，直到任务跑起来探测素材规格才炸，报的还是「素材读不出来」。
    这里靠 `-version` 首行的自报名号判定：真 ffprobe 说 `ffprobe version 6.1.1`，
    冒充者说的是自己原本的名字。跑不起来 / 超时 / 返回码非 0 / 名号对不上，
    一律返回空串（调用方据此当作「不可用」）。
    """
    if not path:
        return ""
    result = run_probe([path, "-version"], timeout=10)
    if result is None or result.returncode != 0:
        logger.warning("工具自检失败（跑不起来）| %s | %s", name, path)
        return ""
    # 首行形如 `<name> version <版本> ...`；stdout 空时退回 stderr（个别版本走 stderr）
    text = result.stdout.strip() or result.stderr.strip()
    head = text.split()
    if len(head) < 2 or head[0].lower() != name.lower() or head[1].lower() != "version":
        logger.warning(
            "工具自检失败（自称是别的工具）| 期望=%s | 实际=%s | %s",
            name,
            " ".join(head[:2]) or "（无输出）",
            path,
        )
        return ""
    return head[2] if len(head) > 2 else "unknown"


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

    Args:
        purpose: 可用时的说明（页面只在不可用时展示 detail，这条是给自检就绪
            的人看的）。
        missing_detail / missing_hint: PATH 里没有这个工具时的说明与修复建议。
        impostor_detail / impostor_hint: 找到了但名号对不上时的说明与修复建议；
            不给就回落到 missing 那两条（对任务的影响是一样的：都用不了）。
    """
    path = find_tool(name)
    if path and verify_tool(name, path):
        return {
            "name": name,
            "ok": True,
            "path": path,
            "detail": purpose,
            "fix_hint": "",
        }
    if path:
        logger.warning("依赖自检：找到的 %s 不是它自己 | %s", name, path)
        return {
            "name": name,
            "ok": False,
            "path": path,
            "detail": impostor_detail or missing_detail,
            "fix_hint": impostor_hint or missing_hint,
        }
    return {
        "name": name,
        "ok": False,
        "path": "",
        "detail": missing_detail,
        "fix_hint": missing_hint,
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
