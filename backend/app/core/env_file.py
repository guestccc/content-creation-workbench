"""`.env` 文件的行级读写：读值、来源判定、原子写回。

这套能力原本长在 `services/subtitle_settings.py` 里（SUBTITLE_VC_ROOT 的回写），
一键成品的 AI 配置（AI_BASE_URL / AI_MODEL / AI_API_KEY）要在页面上填写并写回
同一个 `.env`，「怎么改用户的 .env 而其余内容一字不动」是同一道题：按字节读、
BOM 与换行符风格原样带回、重复键注释掉多余的、临时文件 + os.replace 原子落盘。
抽到这里共用，避免第二份副本只继承代码、不继承教训（教训见各函数注释）。

调用方（subtitle_settings / ai_settings）只提供三样东西：文件路径（模块属性，
可 monkeypatch）、要读写的键名、段头文案。值语义的校验（路径规范化、掩码等）
留在调用方，这里只管「值能不能安全落盘、落进去不破坏别的行」。
"""

import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.core.exceptions import ConflictError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 同一进程内两个并发的 PUT 会抢读-改-写，串行化。一把锁管所有键：
#: 争用的是同一个 `.env` 文件，按键分锁没有收益。
_write_lock = threading.Lock()

#: os.replace 在 Windows 上撞到文件被占用时的重试次数与间隔基数（秒）。
_REPLACE_RETRIES = 3
_REPLACE_BACKOFF_SECONDS = 0.2


def key_line_pattern(key: str) -> re.Pattern:
    """匹配某个键的**未注释**赋值行。

    dotenv 还认 `export` 前缀与冒号写法，一并覆盖；`# KEY=...` 这种注释行
    天然不匹配（`#` 卡在 `^\\s*` 之后）。键名按字面 re.escape，大小写不敏感。
    """
    return re.compile(
        rf"^\s*(?:export\s+)?{re.escape(key)}\s*[:=]\s*(.*?)\s*$", re.IGNORECASE
    )


def read_file(path: Path) -> Tuple[bytes, str]:
    """按字节读文件，把 BOM 单独剥出来交给调用方原样带回。

    按字节而非 `read_text()`：后者默认做 universal newlines 翻译，会把 `\\r\\n`
    读成 `\\n`，再靠 `write_text` 在 Windows 上翻回去 —— 对纯 CRLF 文件恰好
    等价，但会把混合换行悄悄改写，插入新行时也判不准该用哪种终止符。

    Returns:
        (bom, 去掉 BOM 后的文本)。文件不存在时视作 (b"", "")。
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return b"", ""
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    return bom, data[len(bom):].decode("utf-8")


def read_value(path: Path, key: str) -> Optional[str]:
    """读 `.env` 里某个键的未注释值。

    只实现 python-dotenv 语义里我们关心的那部分：无引号的裸值（` #` 之后算
    行内注释）、单/双引号包裹的字面值、`#` 开头的注释行不算数。

    Returns:
        键存在时返回值；空串表示「显式清空」。
        键或文件不存在时返回 None —— 与空串区分开，调用方据此决定要不要回退默认值。
    """
    _bom, text = read_file(path)
    pattern = key_line_pattern(key)
    for line in text.splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        value = match.group(1)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            return value[1:-1]
        return value.split(" #", 1)[0].strip()
    return None


def env_var_shadowing(key: str) -> bool:
    """某个键是否被真正的环境变量占据（优先级高于 .env）。

    大小写不敏感地扫：pydantic-settings 配了 `case_sensitive=False`，连
    `subtitle_vc_root` 这种小写导出也认；而 POSIX 上 `os.environ` 的键是
    区分大小写的，直接 `in` 会漏掉。
    """
    upper = key.upper()
    return any(name.upper() == upper for name in os.environ)


def format_value(value: str) -> str:
    """把值渲染成能安全写进 `.env` 的形态。

    默认裸写（未加引号的值里反斜杠不转义，Windows 路径因此是安全的）；
    值含 `#` 或首尾空白时用**单引号**包裹。

    **绝不用双引号**：python-dotenv 会对双引号值做转义解码，
    `E:\\带货\\...` 这种反斜杠序列和非 ASCII 都会被搞坏。
    """
    if "#" in value or value != value.strip():
        return f"'{value}'"
    return value


def assert_writable(value: str, *, what: str = "值") -> None:
    """拦掉无法安全写进 `.env` 的值。

    换行是最要紧的一条：`\\n` 能往 `.env` 里追加任意额外配置键，
    是一次配置注入（改掉 SUBTITLE_WORKER_ENABLED 之类）。
    引号则是我们自己的转义规则处理不了的字符，宁可直接拒绝。
    """
    bad = [ch for ch in ("\r", "\n", "\x00", "'", '"') if ch in value]
    if bad:
        readable = "、".join(repr(ch) for ch in bad)
        raise ValueError(f"{what}不能包含 {readable} 这类字符")


def _ending_of(line: str) -> str:
    """这一行实际用的换行符；最后一行可能没有（返回空串）。"""
    return line[len(line.rstrip("\r\n")):]


def _is_section_header(line: str, *, mark: str, keys: Tuple[str, ...]) -> bool:
    """这一行是不是目标段的段头。

    判定故意宽松到只看「是个注释、提到了这一节、不是赋值行」：用户手改过
    横线数量（`# ----- ... -----`）或改过措辞都不该导致匹配失败 —— 匹配不上
    的后果是往文件尾**再追加一个段头**，配置文件里出现两处同名段，很乱。
    """
    body = line.strip()
    return (
        body.startswith("#")
        and mark in body
        and not any(key in body for key in keys)
    )


def set_values(
    path: Path,
    values: Dict[str, str],
    *,
    section_mark: str,
    section_header: str,
    log_label: str,
) -> None:
    """把若干键值写进 `.env`，其余内容（注释/空行/CRLF/BOM）原样保留。

    每个键三个分支：
    1. 已有未注释的同名行 → 原地替换；
    2. 没有但有段头 → 插到段头后面（多个新键按传入顺序依次插入）；
    3. 都没有 → 文件尾追加整段（段头 + 全部新键）。

    任何分支都不动其它行 —— 用户在 `.env.example` 里看到的
    `# KEY=...` 那行是文档样例，保持注释状态，两者共存。

    同名行有多条时会**注释掉多余的**：python-dotenv 对同一文件里的重复键是
    后行覆盖前行，只改第一条的话生效的反而是没被改的那条。

    Args:
        path: 目标 `.env`。
        values: 要写入的键值（dict 有序，插入/追加按这个顺序）。
            值须已由调用方规范化；本函数只做值安全性校验与 strip。
        section_mark: 段头的宽松匹配标记（如「视频字幕提取」）。
        section_header: 追加分支使用的完整段头行。
        log_label: 写成功后日志里的称呼（如 SUBTITLE_VC_ROOT / AI 配置）。

    Raises:
        ValueError: 值含换行/引号等无法安全写入 `.env` 的字符。
        ConflictError: 目标文件被其它程序占用（Windows 上编辑器打开着它）。
    """
    items = [(key, value.strip()) for key, value in values.items()]
    for _key, value in items:
        assert_writable(value)

    with _write_lock:
        bom, text = read_file(path)
        newline = "\r\n" if "\r\n" in text else ("\n" if "\n" in text else os.linesep)
        lines = text.splitlines(keepends=True)
        patterns = {key: key_line_pattern(key) for key, _ in items}

        # 1) 原地替换已有的同名行；多余的同名行注释掉
        replaced: Dict[str, bool] = {key: False for key, _ in items}
        for index, line in enumerate(lines):
            body = line.rstrip("\r\n")
            for key, value in items:
                if patterns[key].match(body) is None:
                    continue
                if not replaced[key]:
                    lines[index] = f"{key}={format_value(value)}" + _ending_of(line)
                    replaced[key] = True
                else:
                    # 废掉重复的生效行，保证文件里只有一条才算数
                    lines[index] = f"# {body}" + _ending_of(line)
                break

        missing: List[Tuple[str, str]] = [
            (key, value) for key, value in items if not replaced[key]
        ]
        if missing:
            keys = tuple(key for key, _ in items)
            header_index = next(
                (
                    index
                    for index, line in enumerate(lines)
                    if _is_section_header(line, mark=section_mark, keys=keys)
                ),
                None,
            )
            if header_index is not None:
                # 2) 插到段头后面（保持传入顺序）
                if not lines[header_index].endswith(("\r\n", "\n", "\r")):
                    lines[header_index] += newline
                offset = header_index + 1
                for key, value in missing:
                    lines.insert(offset, f"{key}={format_value(value)}" + newline)
                    offset += 1
            else:
                # 3) 文件尾追加整段
                if lines and lines[-1].strip() != "":
                    lines[-1] = lines[-1].rstrip("\r\n") + newline
                    lines.append(newline)  # 段前空一行
                lines.append(section_header + newline)
                for key, value in missing:
                    lines.append(f"{key}={format_value(value)}" + newline)

        atomic_write(path, bom + "".join(lines).encode("utf-8"), log_label=log_label)


def atomic_write(
    path: Path,
    data: bytes,
    *,
    log_label: str,
    tmp_prefix: str = ".env.",
    tmp_suffix: str = ".local",
) -> None:
    """同目录临时文件 + os.replace 原子落盘。

    临时文件名的**默认值**用 `.env.` 前缀 + `.local` 后缀，恰好命中
    `.gitignore` 里的 `backend/.env.*.local` —— 进程崩在中间也不会把残留文件
    带进 git。写别的文件的调用方（如 services/user_env.py 写 LaunchAgent 的
    plist）传自己的前缀后缀，把这份「同目录临时文件 + fsync + os.replace +
    处处补权限」的教训共用起来。

    Windows 上 os.replace 走 MoveFileExW(MOVEFILE_REPLACE_EXISTING)，
    目标被别的进程打开且没带 FILE_SHARE_DELETE 时会抛 PermissionError
    （VS Code 开着 `.env` 就会命中）。重试几次只救瞬态占用；仍失败就报 409
    让用户关掉编辑器，**不降级**为就地截断重写 —— 同样会失败，还丢了原子性。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(
        prefix=tmp_prefix, suffix=tmp_suffix, dir=str(path.parent)
    )
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        # mkstemp 在 POSIX 上建的是 0600，替换后会把原文件的权限改掉，补回来
        if os.name == "posix" and path.exists():
            os.chmod(tmp_name, stat.S_IMODE(path.stat().st_mode))

        last_error: Optional[PermissionError] = None
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp_name, path)
                logger.info("%s 已写入 %s", log_label, path)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))

        raise ConflictError(
            f"{path} 正被其他程序占用（例如编辑器打开着它），请关闭后重试"
        ) from last_error
    finally:
        try:
            os.unlink(tmp_name)  # replace 成功后 tmp 已不存在，这里自然跳过
        except OSError:
            pass
