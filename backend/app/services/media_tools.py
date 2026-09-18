"""外部媒体工具（ffmpeg / ffprobe）的定位、探测与子进程管理。

这些函数原本长在 `scene_runner.py` 里。智能混剪同样要起 ffmpeg 子进程、
同样要抽缩略图、同样要把 `~/.local/bin` 前插 PATH，于是平移到本模块共用 ——
复制一份的话，「子进程要整组杀」「PATH 要补」这两条踩过坑的规则就会出现两个副本，
迟早只改一处。

`scene_runner` 仍然从本模块把这些名字原样导出，所以 `tests/test_scene_runner.py`
里那些 `from app.services.scene_runner import terminate_process_group` 不受影响。

子进程管理的三条硬规则（两条模块共用，第三条见 scene_runner 顶部）：
1. stdio 必须落文件，绝不 PIPE —— ffmpeg 持续输出，无人读取的管道缓冲写满后
   子进程会永久阻塞在 write 上（表现为「任务跑到一半不动了」）；
2. 必须 start_new_session=True 并整组 kill —— 被调工具自己会拉起子进程，
   只杀直接子进程会留下孤儿继续啃 CPU。Windows 上 Python 忽略
   start_new_session，等价的隔离用下面的 CHILD_CREATION_FLAGS。
"""

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

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
    """在 child_env() 的 PATH 里找可执行文件，找不到返回 None。"""
    return shutil.which(name, path=child_env().get("PATH"))


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
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


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
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=child_env(),
        )
        return round(float(result.stdout.strip()), 3)
    except (OSError, ValueError, subprocess.TimeoutExpired):
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
