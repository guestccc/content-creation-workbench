"""SUBTITLE_VC_ROOT 的持久化（回写 backend/.env）与「这个值从哪来」的判定。

**为什么要有这个模块**：VideoCaptioner 不在本仓库里，装在哪台机器上都不一样。
原来的探测只认 `<工具箱根>/VideoCaptioner` 一个硬编码路径，而真实目录名常带
下载解压留下的后缀（`VideoCaptioner-master` 之类），于是明明装了却探测不到。
现在页面上可以手动指定目录，这个值需要一个落盘的地方 —— 就用 `.env`，
和 `.env.example` 里已有的 `SUBTITLE_VC_ROOT` 是同一个配置项。

**为什么不写数据库 / 单独的 JSON**：`.env` 是这套配置的唯一入口（README、
`.env.example` 都指着它），值放在这里用户能一眼看到、也能直接手改。
`.env` 已被 `.gitignore` 挡住，写进绝对路径不会污染仓库。

依赖方向是单向的，不要反向：
`subtitle_env` / api 层 → 本模块 → `config`。
本模块**不导入** `subtitle_env`（探测逻辑），也**不碰 `settings` 单例**
（除了 sync_from_env_file 那一处受控赋值）—— 「写盘 + 热更新 + 清缓存」的
编排留在 API 层，测试因此不必抢救全局状态。

`backend/.env` 的位置从 `__file__` 推导而非 CWD：`Settings.model_config` 的
`env_file=".env"` 是相对 CWD 的，而所有官方启动方式（README / cw / desktop）
都固定以 `backend/` 为工作目录（`cli/tests/test_services.py` 把这条当契约钉着），
两者因此恒指向同一个文件。
"""

import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from app.core.config import default_vc_root, settings
from app.core.exceptions import ConflictError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 要维护的配置项名（同时也是 .env 里的键名）。
_KEY = "SUBTITLE_VC_ROOT"

#: 写入位置。测试用 monkeypatch 换掉它，所以**必须在调用时读这个模块属性**，
#: 不能在导入时固化进局部变量或函数默认值。
_ENV_PATH: Path = Path(__file__).resolve().parents[2] / ".env"

#: 段头标记与完整段头。匹配时按「以 # ---------- 开头且含这个标记」放宽，
#: 否则用户手改过横线数量后，追加分支会认定「没有段头」而追加出第二个段。
_SECTION_MARK = "视频字幕提取"
_SECTION_HEADER = "# ---------- 视频字幕提取（VideoCaptioner） ----------"

#: 未注释的赋值行。dotenv 还认 `export` 前缀与冒号写法，一并覆盖；
#: `# SUBTITLE_VC_ROOT=...` 这种注释行天然不匹配（`#` 卡在 `^\s*` 之后）。
_KEY_LINE_RE = re.compile(
    r"^\s*(?:export\s+)?SUBTITLE_VC_ROOT\s*[:=]\s*(.*?)\s*$", re.IGNORECASE
)

#: 两个并发的 PUT 会抢读-改-写，串行化。
_write_lock = threading.Lock()

#: os.replace 在 Windows 上撞到文件被占用时的重试次数与间隔基数（秒）。
_REPLACE_RETRIES = 3
_REPLACE_BACKOFF_SECONDS = 0.2


def env_path() -> Path:
    """`.env` 的绝对路径（测试可 monkeypatch 模块属性 `_ENV_PATH` 来改）。"""
    return _ENV_PATH


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def _read() -> Tuple[bytes, str]:
    """按字节读文件，把 BOM 单独剥出来交给调用方原样带回。

    按字节而非 `read_text()`：后者默认做 universal newlines 翻译，会把 `\\r\\n`
    读成 `\\n`，再靠 `write_text` 在 Windows 上翻回去 —— 对纯 CRLF 文件恰好
    等价，但会把混合换行悄悄改写，插入新行时也判不准该用哪种终止符。

    Returns:
        (bom, 去掉 BOM 后的文本)。文件不存在时视作 (b"", "")。
    """
    try:
        data = _ENV_PATH.read_bytes()
    except FileNotFoundError:
        return b"", ""
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    return bom, data[len(bom):].decode("utf-8")


def read_vc_root() -> Optional[str]:
    """读 `.env` 里未注释的 SUBTITLE_VC_ROOT。

    只实现 python-dotenv 语义里我们关心的那部分：无引号的裸值（` #` 之后算
    行内注释）、单/双引号包裹的字面值、`#` 开头的注释行不算数。

    Returns:
        键存在时返回值；空串表示「显式清空」（等价于恢复自动探测）。
        键或文件不存在时返回 None —— 与空串区分开，调用方据此决定要不要回退默认值。
    """
    _bom, text = _read()
    for line in text.splitlines():
        match = _KEY_LINE_RE.match(line)
        if match is None:
            continue
        value = match.group(1)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            return value[1:-1]
        return value.split(" #", 1)[0].strip()
    return None


# --------------------------------------------------------------------------
# 来源判定
# --------------------------------------------------------------------------


def env_var_shadowing() -> bool:
    """SUBTITLE_VC_ROOT 是否被真正的环境变量占据（优先级高于 .env）。

    大小写不敏感地扫：pydantic-settings 配了 `case_sensitive=False`，连
    `subtitle_vc_root` 这种小写导出也认；而 POSIX 上 `os.environ` 的键是
    区分大小写的，直接 `in` 会漏掉。
    """
    return any(key.upper() == _KEY for key in os.environ)


def vc_root_source() -> str:
    """当前生效的 SUBTITLE_VC_ROOT 来自哪一层。

    镜像 pydantic-settings 自己的优先级（环境变量 > .env > 默认值），
    所以语义是「重启后这个值会从哪层来」：

    - `environment`：来自进程环境变量；
    - `env_file`：来自 `.env`（页面写入的也在这一层）；
    - `auto`：没显式配置（含显式清空），靠自动探测。
    """
    if env_var_shadowing():
        return "environment"
    if (read_vc_root() or "").strip():
        return "env_file"
    return "auto"


def shadowing_warning() -> Optional[str]:
    """环境变量盖过 .env 时给用户的一句提醒；不冲突时返回 None。

    这种情况必须说清楚而不是静默：页面上的指定**当前进程立即生效**（我们会
    原地改 settings），但环境变量优先级更高，重启后会被它盖回去 —— 用户
    否则会觉得「我设了怎么又变回去了」。
    """
    if not env_var_shadowing():
        return None
    return (
        f"系统环境变量里已经设置了 {_KEY}，它的优先级高于 .env，"
        "所以写进 .env 的值重启后端后会被它盖回去。"
        "本次指定在当前进程内已生效；若要让页面设置长期有效，请先删除该环境变量。"
    )


def sync_from_env_file() -> bool:
    """把 `.env` 里的值同步进运行中的 settings 单例。

    解决的问题：用户手改了 `.env` 但没重启服务，`settings` 单例还是旧值，
    而 `vc_root_source()` 却会如实报 `env_file` —— 「来源」与「实际生效值」
    脱节。页面上的「重新检测」按钮走这里，就顺带成了「捡起外部修改」的
    万能动作。

    环境变量存在时**不同步**：那是更高优先级的一层，运行中的行为必须和
    重启后一致，否则页面显示的值和重启后的值会来回打架。

    Returns:
        是否真的改动了 settings（供调用方决定要不要清探测缓存）。
    """
    if env_var_shadowing():
        return False
    value = read_vc_root()
    # 键不存在 = 用户把那行删干净了，回退到默认值，而不是留着旧值
    target = value if value is not None else default_vc_root()
    if settings.SUBTITLE_VC_ROOT == target:
        return False
    settings.SUBTITLE_VC_ROOT = target
    logger.info("SUBTITLE_VC_ROOT 已从 %s 同步为 %r", _ENV_PATH, target)
    return True


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------


def _format_value(value: str) -> str:
    """把值渲染成能安全写进 `.env` 的形态。

    默认裸写（未加引号的值里反斜杠不转义，Windows 路径因此是安全的）；
    值含 `#` 或首尾空白时用**单引号**包裹。

    **绝不用双引号**：python-dotenv 会对双引号值做转义解码，
    `E:\\带货\\...` 这种反斜杠序列和非 ASCII 都会被搞坏。
    """
    if "#" in value or value != value.strip():
        return f"'{value}'"
    return value


def _assert_writable_value(value: str) -> None:
    """拦掉无法安全写进 `.env` 的值。

    换行是最要紧的一条：`\\n` 能往 `.env` 里追加任意额外配置键，
    是一次配置注入（改掉 SUBTITLE_WORKER_ENABLED 之类）。
    引号则是我们自己的转义规则处理不了的字符，宁可直接拒绝。
    """
    bad = [ch for ch in ("\r", "\n", "\x00", "'", '"') if ch in value]
    if bad:
        readable = "、".join(repr(ch) for ch in bad)
        raise ValueError(f"路径不能包含 {readable} 这类字符")


def _ending_of(line: str) -> str:
    """这一行实际用的换行符；最后一行可能没有（返回空串）。"""
    return line[len(line.rstrip("\r\n")):]


def _is_section_header(line: str) -> bool:
    """这一行是不是「视频字幕提取」的段头。

    判定故意宽松到只看「是个注释、提到了这一节、不是赋值行」：用户手改过
    横线数量（`# ----- ... -----`）或改过措辞都不该导致匹配失败 —— 匹配不上
    的后果是往文件尾**再追加一个段头**，配置文件里出现两处同名段，很乱。
    """
    body = line.strip()
    return body.startswith("#") and _SECTION_MARK in body and _KEY not in body


def write_vc_root(value: str) -> None:
    """把 SUBTITLE_VC_ROOT 写进 `.env`，其余内容（注释/空行/CRLF/BOM）原样保留。

    三个分支：
    1. 已有未注释的同名行 → 原地替换；
    2. 没有但有「视频字幕提取」段头 → 插到段头后面；
    3. 都没有 → 文件尾追加整段。

    任何分支都不动其它行 —— 用户在 `.env.example` 里看到的
    `# SUBTITLE_VC_ROOT=...` 那行是文档样例，保持注释状态，两者共存。

    同名行有多条时会**注释掉多余的**：python-dotenv 对同一文件里的重复键是
    后行覆盖前行，只改第一条的话生效的反而是没被改的那条。

    Args:
        value: 要写入的值；已由调用方规范化。空串表示「恢复自动探测」。

    Raises:
        ValueError: 值含换行/引号等无法安全写入 `.env` 的字符。
        ConflictError: 目标文件被其它程序占用（Windows 上编辑器打开着它）。
    """
    value = value.strip()
    _assert_writable_value(value)

    with _write_lock:
        bom, text = _read()
        newline = "\r\n" if "\r\n" in text else ("\n" if "\n" in text else os.linesep)
        lines = text.splitlines(keepends=True)
        replacement = f"{_KEY}={_format_value(value)}"

        replaced = False
        for index, line in enumerate(lines):
            body = line.rstrip("\r\n")
            if _KEY_LINE_RE.match(body) is None:
                continue
            if not replaced:
                lines[index] = replacement + _ending_of(line)
                replaced = True
            else:
                # 废掉重复的生效行，保证文件里只有一条才算数
                lines[index] = f"# {body}" + _ending_of(line)

        if not replaced:
            header_index = next(
                (
                    index
                    for index, line in enumerate(lines)
                    if _is_section_header(line)
                ),
                None,
            )
            if header_index is not None:
                if not lines[header_index].endswith(("\r\n", "\n", "\r")):
                    lines[header_index] += newline
                lines.insert(header_index + 1, replacement + newline)
            else:
                if lines and lines[-1].strip() != "":
                    lines[-1] = lines[-1].rstrip("\r\n") + newline
                    lines.append(newline)  # 段前空一行
                lines.append(_SECTION_HEADER + newline)
                lines.append(replacement + newline)

        _atomic_write(bom + "".join(lines).encode("utf-8"))


def _atomic_write(data: bytes) -> None:
    """同目录临时文件 + os.replace 原子落盘。

    临时文件名用 `.env.` 前缀 + `.local` 后缀，恰好命中 `.gitignore` 里的
    `backend/.env.*.local` —— 进程崩在中间也不会把残留文件带进 git。

    Windows 上 os.replace 走 MoveFileExW(MOVEFILE_REPLACE_EXISTING)，
    目标被别的进程打开且没带 FILE_SHARE_DELETE 时会抛 PermissionError
    （VS Code 开着 `.env` 就会命中）。重试几次只救瞬态占用；仍失败就报 409
    让用户关掉编辑器，**不降级**为就地截断重写 —— 同样会失败，还丢了原子性。
    """
    _ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(
        prefix=".env.", suffix=".local", dir=str(_ENV_PATH.parent)
    )
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        # mkstemp 在 POSIX 上建的是 0600，替换后会把原文件的权限改掉，补回来
        if os.name == "posix" and _ENV_PATH.exists():
            os.chmod(tmp_name, stat.S_IMODE(_ENV_PATH.stat().st_mode))

        last_error: Optional[PermissionError] = None
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp_name, _ENV_PATH)
                logger.info("SUBTITLE_VC_ROOT 已写入 %s", _ENV_PATH)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))

        raise ConflictError(
            f"{_ENV_PATH} 正被其他程序占用（例如编辑器打开着它），请关闭后重试"
        ) from last_error
    finally:
        try:
            os.unlink(tmp_name)  # replace 成功后 tmp 已不存在，这里自然跳过
        except OSError:
            pass


def normalize_vc_root(raw: str) -> str:
    """把用户传来的路径规范化：去首尾空白、展开 `~`、转绝对路径。

    统一规范化后再落盘与返回，避免 `~`、大小写、分隔符的写法差异干扰
    「显式配置」与「自动发现」之间的去重判断。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    return str(Path(text).expanduser().resolve())
