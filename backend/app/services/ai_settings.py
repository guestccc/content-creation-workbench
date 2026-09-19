"""AI 配置（AI_BASE_URL / AI_MODEL / AI_API_KEY）的页面读写与 .env 回写。

一键成品的文案生成要调 DeepSeek（OpenAI 兼容接口），key 在页面上填写、
写回 `backend/.env` —— 与 SUBTITLE_VC_ROOT 同一套落地方式与理由
（`.env` 是这套配置的唯一入口，见 subtitle_settings.py 的模块 docstring）。
行级读写机制共用 `app/core/env_file.py`，本模块只管 AI 三键的**值语义**：

- base_url / model：必填、可展示、可改写；
- api_key：**只写不读**。读接口一律返回掩码（mask_key），完整 key 不出 `.env`；
  写入时**空串 = 不动它**，避免「只想改个模型却把 key 抹了」。

依赖方向与 subtitle_settings 相同：api 层 → 本模块 → env_file / config；
本模块不碰探测逻辑，settings 单例只在 sync_from_env_file 里受控赋值。
"""

from pathlib import Path
from typing import Dict, List, Optional

from app.core import env_file
from app.core.config import settings
from app.core.logging import get_logger
from app.services.ai_client import mask_key

logger = get_logger(__name__)

#: 本模块维护的三个键（顺序即写入 .env 的顺序）。
_KEY_BASE_URL = "AI_BASE_URL"
_KEY_MODEL = "AI_MODEL"
_KEY_API_KEY = "AI_API_KEY"
_AI_KEYS = (_KEY_BASE_URL, _KEY_MODEL, _KEY_API_KEY)

#: 写入位置。与 subtitle_settings 同一约定：调用时读模块属性，测试可 monkeypatch。
_ENV_PATH: Path = Path(__file__).resolve().parents[2] / ".env"

#: 段头标记与完整段头（与 config.py 里「AI（一键成品的文案生成）」横幅同文案）。
_SECTION_MARK = "一键成品的文案生成"
_SECTION_HEADER = "# ---------- AI（一键成品的文案生成） ----------"


def env_path() -> Path:
    """`.env` 的绝对路径（测试可 monkeypatch 模块属性 `_ENV_PATH` 来改）。"""
    return _ENV_PATH


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def read_env_values() -> Dict[str, Optional[str]]:
    """三个键在 `.env` 里的当前值（None = 键不存在，空串 = 显式清空）。"""
    return {key: env_file.read_value(_ENV_PATH, key) for key in _AI_KEYS}


def shadowed_keys() -> List[str]:
    """被真实环境变量占据的键（优先级高于 .env，写进去重启后会被盖回）。"""
    return [key for key in _AI_KEYS if env_file.env_var_shadowing(key)]


def read_ai_settings() -> dict:
    """页面「AI 配置」弹窗的读取模型。

    base_url / model 给的是 settings 里的**生效值**（表单据此回填）；
    api_key 只给「有没有 + 掩码」，完整值不出后端。
    """
    shadowed = shadowed_keys()
    warning = ""
    if shadowed:
        warning = (
            f"系统环境变量里已经设置了 {'、'.join(shadowed)}，它的优先级高于 .env，"
            "写进 .env 的值重启后端后会被盖回去；本次修改在当前进程内已生效。"
        )
    return {
        "base_url": settings.AI_BASE_URL,
        "model": settings.AI_MODEL,
        "api_key_present": bool(settings.AI_API_KEY.strip()),
        "api_key_masked": mask_key(settings.AI_API_KEY),
        "shadowed_keys": shadowed,
        "warning": warning,
    }


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------


def write_ai_settings(*, base_url: str, model: str, api_key: str = "") -> None:
    """把 AI 配置写进 `.env`（api_key 留空 = 保持原值不动）。

    Args:
        base_url: OpenAI 兼容端点（如 https://api.deepseek.com/v1），必填。
        model: 模型名（如 deepseek-chat），必填。
        api_key: 新 key；空串表示「不改」（读接口也只给掩码，
            所以页面回填不了原值，留空必须等于不动）。

    Raises:
        ValueError: base_url / model 为空，或任一值含无法安全落盘的字符。
        ConflictError: `.env` 被其它程序占用。
    """
    base_url = base_url.strip().rstrip("/")
    model = model.strip()
    if not base_url:
        raise ValueError("AI_BASE_URL 不能为空（默认 https://api.deepseek.com/v1）")
    if not model:
        raise ValueError("AI_MODEL 不能为空（默认 deepseek-chat）")

    values: Dict[str, str] = {_KEY_BASE_URL: base_url, _KEY_MODEL: model}
    if api_key.strip():
        values[_KEY_API_KEY] = api_key.strip()

    env_file.set_values(
        _ENV_PATH,
        values,
        section_mark=_SECTION_MARK,
        section_header=_SECTION_HEADER,
        log_label="AI 配置",
    )


def sync_from_env_file() -> bool:
    """把 `.env` 里的 AI 三键同步进运行中的 settings 单例。

    与 subtitle_settings.sync_from_env_file 同一套理由与约束：被环境变量
    占据的键不同步（运行中行为必须和重启后一致）；键被删掉时回退默认值。

    Returns:
        是否真的改动了 settings。
    """
    shadowed = set(shadowed_keys())
    values = read_env_values()
    defaults = {
        _KEY_BASE_URL: "https://api.deepseek.com/v1",
        _KEY_MODEL: "deepseek-chat",
        _KEY_API_KEY: "",
    }
    changed = False
    for key in _AI_KEYS:
        if key in shadowed:
            continue
        value = values[key]
        target = value if value is not None else defaults[key]
        if getattr(settings, key) != target:
            setattr(settings, key, target)
            changed = True
    if changed:
        logger.info(
            "AI 配置已从 %s 同步（base_url=%s, model=%s, key=%s）",
            _ENV_PATH,
            settings.AI_BASE_URL,
            settings.AI_MODEL,
            mask_key(settings.AI_API_KEY) or "（空）",
        )
    return changed
