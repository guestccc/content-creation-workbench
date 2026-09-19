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
`subtitle_env` / api 层 → 本模块 → `app.core.env_file` → `config`。
本模块**不导入** `subtitle_env`（探测逻辑），也**不碰 `settings` 单例**
（除了 sync_from_env_file 那一处受控赋值）—— 「写盘 + 热更新 + 清缓存」的
编排留在 API 层，测试因此不必抢救全局状态。

`.env` 的行级读写机制（按字节读、BOM/CRLF 原样带回、原子替换、重复键处理）
在 `app/core/env_file.py` 里与 ai_settings 共用；这里只剩
SUBTITLE_VC_ROOT 的**值语义**：路径规范化、来源判定、settings 同步。

`backend/.env` 的位置从 `__file__` 推导而非 CWD：`Settings.model_config` 的
`env_file=".env"` 是相对 CWD 的，而所有官方启动方式（README / cw / desktop）
都固定以 `backend/` 为工作目录（`cli/tests/test_services.py` 把这条当契约钉着），
两者因此恒指向同一个文件。
"""

from pathlib import Path
from typing import Optional

from app.core import env_file
from app.core.config import default_vc_root, settings
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


def env_path() -> Path:
    """`.env` 的绝对路径（测试可 monkeypatch 模块属性 `_ENV_PATH` 来改）。"""
    return _ENV_PATH


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def read_vc_root() -> Optional[str]:
    """读 `.env` 里未注释的 SUBTITLE_VC_ROOT。

    Returns:
        键存在时返回值；空串表示「显式清空」（等价于恢复自动探测）。
        键或文件不存在时返回 None —— 与空串区分开，调用方据此决定要不要回退默认值。
    """
    return env_file.read_value(_ENV_PATH, _KEY)


# --------------------------------------------------------------------------
# 来源判定
# --------------------------------------------------------------------------


def env_var_shadowing() -> bool:
    """SUBTITLE_VC_ROOT 是否被真正的环境变量占据（优先级高于 .env）。"""
    return env_file.env_var_shadowing(_KEY)


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


def write_vc_root(value: str) -> None:
    """把 SUBTITLE_VC_ROOT 写进 `.env`，其余内容（注释/空行/CRLF/BOM）原样保留。

    Args:
        value: 要写入的值；已由调用方规范化。空串表示「恢复自动探测」。

    Raises:
        ValueError: 值含换行/引号等无法安全写入 `.env` 的字符。
        ConflictError: 目标文件被其它程序占用（Windows 上编辑器打开着它）。
    """
    env_file.set_values(
        _ENV_PATH,
        {_KEY: value},
        section_mark=_SECTION_MARK,
        section_header=_SECTION_HEADER,
        log_label=_KEY,
    )


def normalize_vc_root(raw: str) -> str:
    """把用户传来的路径规范化：去首尾空白、展开 `~`、转绝对路径。

    统一规范化后再落盘与返回，避免 `~`、大小写、分隔符的写法差异干扰
    「显式配置」与「自动发现」之间的去重判断。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    return str(Path(text).expanduser().resolve())
