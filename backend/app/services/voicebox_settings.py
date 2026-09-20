"""VOICEBOX_BASE_URL 的持久化（回写 backend/.env）与「这个值从哪来」的判定。

**为什么要有这个模块**：Voicebox 装在用户自己机器上，服务地址默认是
`http://127.0.0.1:17493`，但远程 GPU 部署（Voicebox 的 Remote Mode / Docker）
时地址会变。页面上要能改，这个值就需要一个落盘的地方 —— 就用 `.env`，
和 config.py 里的 `VOICEBOX_BASE_URL` 是同一个配置项。

**为什么不写数据库 / 单独的 JSON**：与 subtitle_settings 同一条理由 ——
`.env` 是这套配置的唯一入口，值放在这里用户能一眼看到、也能直接手改；
`.env` 已被 `.gitignore` 挡住，写进地址不会污染仓库。

依赖方向是单向的，不要反向：
`voicebox_env` / api 层 → 本模块 → `app.core.env_file` → `config`。
本模块**不导入** `voicebox_env`（探测逻辑），也**不碰 `settings` 单例**
（除了 sync_from_env_file 那一处受控赋值）——「写盘 + 热更新 + 清缓存」的
编排留在 API 层，测试因此不必抢救全局状态。

`.env` 的行级读写机制（按字节读、BOM/CRLF 原样带回、原子替换、重复键处理）
在 `app/core/env_file.py` 里与 ai_settings / subtitle_settings 共用；
这里只剩 VOICEBOX_BASE_URL 的**值语义**：地址规范化、来源判定、settings 同步。
"""

from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from app.core import env_file
from app.core.config import default_voicebox_base_url, settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 要维护的配置项名（同时也是 .env 里的键名）。
_KEY = "VOICEBOX_BASE_URL"

#: 写入位置。测试用 monkeypatch 换掉它，所以**必须在调用时读这个模块属性**，
#: 不能在导入时固化进局部变量或函数默认值。
_ENV_PATH: Path = Path(__file__).resolve().parents[2] / ".env"

#: 段头标记与完整段头。匹配时按「以 # ---------- 开头且含这个标记」放宽，
#: 否则用户手改过横线数量后，追加分支会认定「没有段头」而追加出第二个段。
_SECTION_MARK = "智能配音"
_SECTION_HEADER = "# ---------- 智能配音（Voicebox） ----------"

#: 认得出的协议。Voicebox 是 HTTP 服务，别的协议（file://、ftp://）没有意义。
_ALLOWED_SCHEMES = ("http", "https")


def env_path() -> Path:
    """`.env` 的绝对路径（测试可 monkeypatch 模块属性 `_ENV_PATH` 来改）。"""
    return _ENV_PATH


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def read_base_url() -> Optional[str]:
    """读 `.env` 里未注释的 VOICEBOX_BASE_URL。

    Returns:
        键存在时返回值；空串表示「显式清空」（等价于恢复默认地址）。
        键或文件不存在时返回 None —— 与空串区分开，调用方据此决定要不要回退默认值。
    """
    return env_file.read_value(_ENV_PATH, _KEY)


# --------------------------------------------------------------------------
# 来源判定
# --------------------------------------------------------------------------


def env_var_shadowing() -> bool:
    """VOICEBOX_BASE_URL 是否被真正的环境变量占据（优先级高于 .env）。"""
    return env_file.env_var_shadowing(_KEY)


def base_url_source() -> str:
    """当前生效的 VOICEBOX_BASE_URL 来自哪一层。

    镜像 pydantic-settings 自己的优先级（环境变量 > .env > 默认值），
    所以语义是「重启后这个值会从哪层来」：

    - `environment`：来自进程环境变量；
    - `env_file`：来自 `.env`（页面写入的也在这一层）；
    - `default`：没显式配置（含显式清空），用 config 里的默认地址。

    与 subtitle_settings 的 `auto` 不同：那里「没配」要靠扫盘探测，这里
    「没配」就是一个确定有效的默认地址，所以叫 default 而不是 auto。
    """
    if env_var_shadowing():
        return "environment"
    if (read_base_url() or "").strip():
        return "env_file"
    return "default"


#: 算「本机」的主机名。`::1` 是 IPv6 回环，urlparse 会把它放在 hostname 里。
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def is_local_base_url() -> bool:
    """当前生效地址是不是本机。

    「设置镜像」和「重启 Voicebox」这两个动作都只能作用在**跑着 Voicebox 的那台
    机器**上：环境变量写在本地用户的会话 / 注册表里，进程也是本机进程。地址指向
    远程 GPU 机器（Voicebox 的 Remote Mode / Docker）时这两个动作没有意义 ——
    页面该把按钮藏起来，并如实说明「去那台机器上设置」。
    """
    host = (urlparse(settings.VOICEBOX_BASE_URL).hostname or "").lower()
    return host in _LOCAL_HOSTS


def shadowing_warning() -> Optional[str]:
    """环境变量盖过 .env 时给用户的一句提醒；不冲突时返回 None。

    这种情况必须说清楚而不是静默：页面上的指定**当前进程立即生效**（我们会
    原地改 settings），但环境变量优先级更高，重启后会被它盖回去。
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

    解决的问题：用户手改了 `.env` 但没重启服务，`settings` 单例还是旧值。
    页面上的「重新检测」按钮走这里，就顺带成了「捡起外部修改」的万能动作。

    环境变量存在时**不同步**：那是更高优先级的一层，运行中的行为必须和
    重启后一致，否则页面显示的值和重启后的值会来回打架。

    Returns:
        是否真的改动了 settings（供调用方决定要不要清探测缓存）。
    """
    if env_var_shadowing():
        return False
    value = read_base_url()
    # 键不存在 = 用户把那行删干净了，回退到默认值，而不是留着旧值
    target = value if (value or "").strip() else default_voicebox_base_url()
    if settings.VOICEBOX_BASE_URL == target:
        return False
    settings.VOICEBOX_BASE_URL = target
    logger.info("VOICEBOX_BASE_URL 已从 %s 同步为 %r", _ENV_PATH, target)
    return True


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------


def write_base_url(value: str) -> None:
    """把 VOICEBOX_BASE_URL 写进 `.env`，其余内容（注释/空行/CRLF/BOM）原样保留。

    Args:
        value: 要写入的值；已由调用方规范化。空串表示「恢复默认地址」。

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


def normalize_base_url(raw: str) -> str:
    """把用户输入的服务地址规范化成 `scheme://host[:port]`，非法值直接报错。

    处理三类常见写法：没带协议的 `127.0.0.1:17493`（补 http://）、
    尾部多余的 `/`（去掉）、以及中间/两端混进空白的粘贴结果。

    Raises:
        ValueError: 协议不是 http/https，或者没有主机名 —— 后端地址写错时
            宁可当场报错，也不要等生成时才抛「连不上」，那样用户会以为是
            Voicebox 没开。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    # 中间混进空白（换行最常见）必须在这里挡住：urlparse 会把 \n 悄悄吃掉再解析，
    # 于是 hostname 检查照过，而我们返回的仍是带换行的原文 —— 那会被写进 .env
    # 并注入出一行新配置。地址里本来也不允许有空格。
    if any(char.isspace() for char in text):
        raise ValueError("服务地址里不能有空格或换行，请照 http://127.0.0.1:17493 的格式填")
    if "://" not in text:
        text = f"http://{text}"
    text = text.rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"服务地址只支持 http / https：（当前是 {parsed.scheme or '空'}）{text}"
        )
    if not parsed.hostname:
        raise ValueError(f"服务地址里没有主机名，请照 http://127.0.0.1:17493 的格式填：{text}")
    return text
