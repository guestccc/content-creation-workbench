"""模型下载源镜像（`HF_ENDPOINT`）的策略层。

与 `app/core/env_file.py` ↔ `services/voicebox_settings.py` 是同一套切分：
`services/user_env.py` 是「怎么设一个用户级环境变量」的**通用能力**（两个平台两套
落点），本模块是「设哪个键、设成什么、现在算不算已生效」的**业务判断**。

为什么必须重启 Voicebox
=======================
`HF_ENDPOINT` 是 `huggingface_hub` **在导入时**读进模块变量的，也就是 Voicebox 的
`voicebox-server` 进程**启动那一刻**定死。改完环境变量不重启，那个进程手里还是旧值 ——
用户会以为「设了没用」。所以页面上设置完之后必须跟着一个「重启 Voicebox」。

为什么写死一个镜像地址
======================
产品决策：页面上不做输入框。可选镜像是个会变的集合（可用性、速度、是否兼容
HuggingFace 的 API 形状都会变），做成下拉框等于把「挑哪个」这个判断推回给用户。
写死一个实测可用的（HF-API 兼容、文件校验和与官方一致），需要换时改这一处常量。
"""

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from app.core.logging import get_logger
from app.services import user_env

logger = get_logger(__name__)

#: 要设置的键。HuggingFace 全家（transformers / huggingface_hub / diffusers）
#: 都认这个变量作为下载端点。
HF_MIRROR_KEY = "HF_ENDPOINT"

#: 推荐镜像。实测：HF-API 兼容（`/api/models` 与 `/api/models/{id}` 都返回合规 JSON）、
#: 大文件能稳定拉满带宽、下下来的分片校验和与官方一致。
#: **不要换成 ModelScope** —— 它更快，但 API 形状不是 HuggingFace 那套，
#: 设成 HF_ENDPOINT 会让 huggingface_hub 直接解析失败。
HF_MIRROR_URL = "https://hf-api.gitee.com"


@dataclass(frozen=True)
class MirrorStatus:
    """镜像的当前状态（给自检与页面用）。"""

    supported: bool
    """当前系统能不能由本工具设置（见 user_env.supported）。"""

    value: Optional[str]
    """当前 `HF_ENDPOINT`；None 表示未设置。"""

    persistent: bool
    """注销 / 重启后是否还在。false 且 value 有值时，是一次「设了但不持久」的写入。"""

    error: str = ""
    """读取失败时的一句话说明；读成功时为空串。**不抛异常**：自检里读不到
    环境变量不该让整个自检崩掉，页面只是少一条信息。"""

    @property
    def is_recommended(self) -> bool:
        """当前值是不是本模块推荐的那个镜像。"""
        return is_recommended_value(self.value)


def _normalize(value: Optional[str]) -> str:
    """比较用的规范化：去尾部斜杠、小写、去掉两端空白。"""
    return (value or "").strip().rstrip("/").lower()


def is_recommended_value(value: Optional[str]) -> bool:
    """某个值是不是推荐镜像。

    比尾部斜杠与大小写宽松：用户手敲的 `https://hf-api.gitee.com/` 与常量是
    同一个东西，不该在页面上显示成「未设置镜像」而反复劝他再点一次。
    """
    return _normalize(value) == _normalize(HF_MIRROR_URL)


def _unsupported_status() -> MirrorStatus:
    return MirrorStatus(supported=False, value=None, persistent=False)


def status() -> MirrorStatus:
    """读当前镜像状态。**任何失败都退化成「读不到」，绝不抛异常。**

    兜的是 `Exception` 而不只是 `UserEnvError`：调用方
    （`voicebox_env._safe_mirror_status`）**就是照「它绝不抛」这个承诺写的**，
    它自己没有再包一层 try —— 这里漏掉一种异常类型，自检就变成 500 了。
    计划外的失败（注册表权限、加载的 DLL 缺函数……）也算「读不到」，
    页面上少一条信息，好过整个页面报错。
    """
    if not user_env.supported():
        return _unsupported_status()
    try:
        current = user_env.read_state(HF_MIRROR_KEY)
    except user_env.UserEnvError as exc:
        logger.warning("读取 %s 失败：%s", HF_MIRROR_KEY, exc.detail or exc.user_message)
        return MirrorStatus(
            supported=True, value=None, persistent=False, error=exc.user_message
        )
    except Exception as exc:  # noqa: BLE001 - 见 docstring：这里的契约是不抛
        logger.warning("读取 %s 时出现意料之外的失败：%s", HF_MIRROR_KEY, exc)
        return MirrorStatus(supported=True, value=None, persistent=False, error="")
    return MirrorStatus(
        supported=True, value=current.value, persistent=current.persistent
    )


def current_value() -> Optional[str]:
    """给「重启 Voicebox 时拼子进程环境」用的当前值。

    与 `status()` 分开的理由：重启路径**不能被读环境变量这件事挡住**。
    读不到就按「没设镜像」处理（重启照做），比让整个重启失败强。
    异常范围同 `status()`：只认 `UserEnvError` 会让别的失败把重启一起带走。
    """
    if not user_env.supported():
        return None
    try:
        return user_env.read_state(HF_MIRROR_KEY).value
    except user_env.UserEnvError as exc:
        logger.warning("重启前读取 %s 失败，按未设置处理：%s", HF_MIRROR_KEY, exc.user_message)
        return None
    except Exception as exc:  # noqa: BLE001 - 见 docstring：这里的契约是不抛
        logger.warning("重启前读取 %s 时出现意料之外的失败，按未设置处理：%s", HF_MIRROR_KEY, exc)
        return None


def enable() -> MirrorStatus:
    """把下载源设成推荐镜像（幂等），返回设置后的状态。

    Raises:
        user_env.UserEnvError: 平台不支持 / 写入失败。
    """
    user_env.set_value(HF_MIRROR_KEY, HF_MIRROR_URL)
    return status()


def disable() -> MirrorStatus:
    """清除下载源、恢复 HuggingFace 官方默认（幂等），返回清除后的状态。

    Raises:
        user_env.UserEnvError: 平台不支持 / 清除失败。
    """
    user_env.clear_value(HF_MIRROR_KEY)
    return status()


def is_customized(value: Optional[str]) -> bool:
    """当前值存在、但不是推荐的镜像（用户自己设了别的源）。

    页面据此提醒一句，但**不覆盖**用户的选择 —— 他可能有自己的内网镜像。
    """
    return bool((value or "").strip()) and not is_recommended_value(value)


def host_of(value: Optional[str]) -> str:
    """取地址里的主机名（只用于文案，解析不出来就原样返回）。"""
    text = (value or "").strip()
    if not text:
        return ""
    return urlsplit(text).hostname or text
