"""一键成品·口播语速（FINALCUT_CHARS_PER_SECOND）的页面读写与 .env 回写。

为什么单独一个模块、不并进 `ai_settings.py`：那个模块管的是「接哪个 AI 服务」
（base_url / model / api_key，三键是一个整体，key 还有掩码与「留空 = 不改」的
特殊语义）；语速是**内容量纲**，跟接哪个模型无关，校验规则（数值区间）与展示
位置也都不同。行级读写机制共用 `app/core/env_file.py`，本模块只管语速的**值语义**。

语速的方向：**每秒钟念几个字**。5.8 表示一秒念 5.8 个字。用户拿一段已知字数的
文案在剪映里生成配音、量出实际秒数即可反算（87 字念了 15 秒 → 87 / 15 ≈ 5.8），
页面「AI 配置与口播语速」弹窗里填一次。默认值见 config.default_chars_per_second()。

依赖方向与 ai_settings 相同：api 层 → 本模块 → env_file / config；
本模块不碰探测逻辑，settings 单例只在 sync_from_env_file 里受控赋值。
"""

from pathlib import Path
from typing import Optional

from app.core import env_file
from app.core.config import default_chars_per_second, settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 本模块维护的键。
_KEY = "FINALCUT_CHARS_PER_SECOND"

#: 写入位置。与 ai_settings 同一约定：调用时读模块属性，测试可 monkeypatch。
_ENV_PATH: Path = Path(__file__).resolve().parents[2] / ".env"

#: 段头标记与完整段头。
#:
#: 标记取「口播语速」这四个字，**绝不能取「一键成品」**：env_file._is_section_header
#: 是子串匹配（core/env_file.py:132-144），而 .env 里已有一个
#: `# ---------- AI（一键成品的文案生成） ----------` 段头，标记里带「一键成品」
#: 会把它误判成本段的段头，语速键就被插进 AI 段里了（同样的理由反过来：本段标记
#: 也不能出现在 AI 段头里，取「口播语速」天然不冲突）。测试里有专门用例钉住这条。
_SECTION_MARK = "口播语速"
_SECTION_HEADER = "# ---------- 一键成品（口播语速） ----------"

#: 合法区间。慢于 1 字/秒或快于 15 字/秒都不是正常口播，八成是填错了
#: （比如把「念了多少秒」填进了「语速」这一栏）。
MIN_CHARS_PER_SECOND = 1.0
MAX_CHARS_PER_SECOND = 15.0


def env_path() -> Path:
    """`.env` 的绝对路径（测试可 monkeypatch 模块属性 `_ENV_PATH` 来改）。"""
    return _ENV_PATH


# --------------------------------------------------------------------------
# 值语义
# --------------------------------------------------------------------------


def normalize_chars_per_second(value: object) -> float:
    """校验并归一化语速，返回保留两位小数的浮点数。

    范围校验放在这里、**不放 pydantic 的 field_validator**：热同步
    （sync_from_env_file）是直接 setattr 到 settings 单例，而 Settings 没开
    validate_assignment，那条路绕过 pydantic 的所有校验 —— 只有放在这个模块里
    才能让「页面写入」和「.env 热同步」两条路都被拦住。

    Raises:
        ValueError: 不是数字，或超出 MIN/MAX 区间（API 层统一转 400）。
    """
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError("口播语速必须是数字（字/秒）") from exc
    if not (MIN_CHARS_PER_SECOND <= number <= MAX_CHARS_PER_SECOND):
        raise ValueError(
            f"口播语速要在 {MIN_CHARS_PER_SECOND}–{MAX_CHARS_PER_SECOND} 字/秒之间"
            f"（当前填的是 {number}）"
        )
    return round(number, 2)


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def read_chars_per_second() -> Optional[float]:
    """`.env` 里的语速；键不存在 / 空 / 不合法都返回 None（= 用默认值）。

    不合法时**不抛异常**：这是「读」路径，热同步在每次点「重新检测」时都会跑，
    用户手改坏了 `.env` 不该让整个环境接口 500 —— 记一条 warning 退回默认值。
    """
    raw = env_file.read_value(_ENV_PATH, _KEY)
    if raw is None or not raw.strip():
        return None
    try:
        return normalize_chars_per_second(raw)
    except ValueError:
        logger.warning("%s 里的 %s=%r 不合法，本次按默认值处理", _ENV_PATH, _KEY, raw)
        return None


def shadowed() -> bool:
    """本键是否被真实环境变量占据（优先级高于 .env）。"""
    return env_file.env_var_shadowing(_KEY)


def read_finalcut_settings() -> dict:
    """页面读取模型里「语速」那一块。

    字段名带 `chars_per_second_` 前缀是**故意的**：这个 dict 会和
    ai_settings.read_ai_settings() 合并后一起喂给同一个响应模型，而后者已经有
    一个 `warning` 键，这里直接叫 `warning` 会被覆盖掉（键冲突静默丢数据）。
    """
    return {
        "chars_per_second": float(settings.FINALCUT_CHARS_PER_SECOND),
        "chars_per_second_default": default_chars_per_second(),
        "chars_per_second_warning": (
            f"系统环境变量里已经设置了 {_KEY}，它的优先级高于 .env，"
            "写进 .env 的值重启后端后会被盖回去；本次修改在当前进程内已生效。"
            if shadowed()
            else ""
        ),
    }


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------


def write_chars_per_second(value: object) -> float:
    """把语速写进 `.env`，返回归一化后的值。

    Raises:
        ValueError: 值不合法（见 normalize_chars_per_second）。
        ConflictError: `.env` 被其它程序占用（编辑器开着它）。
    """
    number = normalize_chars_per_second(value)
    env_file.set_values(
        _ENV_PATH,
        {_KEY: repr(number)},  # repr(float) 无引号无空格，format_value 会裸写
        section_mark=_SECTION_MARK,
        section_header=_SECTION_HEADER,
        log_label="一键成品·口播语速",
    )
    return number


def sync_from_env_file() -> bool:
    """把 `.env` 里的语速同步进运行中的 settings 单例。

    与 ai_settings.sync_from_env_file 同一套理由与约束：被环境变量占据的键不同步
    （运行中行为必须和重启后一致）；键被删掉/改坏时回退默认值。

    **必须 float() 再 setattr**：env_file.read_value 给的是字符串，直接 setattr
    会让 `duration_seconds * settings.FINALCUT_CHARS_PER_SECOND` 变成
    `float * str` → TypeError，而且是任务跑起来才炸（不在请求路径上）。

    Returns:
        是否真的改动了 settings。
    """
    if shadowed():
        return False
    value = read_chars_per_second()
    target = default_chars_per_second() if value is None else value
    if float(settings.FINALCUT_CHARS_PER_SECOND) != target:
        settings.FINALCUT_CHARS_PER_SECOND = target
        logger.info("口播语速已从 %s 同步：%s 字/秒", _ENV_PATH, target)
        return True
    return False
