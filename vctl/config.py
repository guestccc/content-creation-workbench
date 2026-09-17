"""
vct 自身的持久化配置。

存放位置：~/.config/vct/config.json（遵守 XDG_CONFIG_HOME）

这里存的都是「下次别再问我一遍」的偏好设置：常用目录、默认参数、
最近处理过的文件。注意这跟两个工具各自的配置是分开的：
* VideoCaptioner 的配置在  ~/.config/videocaptioner/config.toml
* video-subtitle-remover 的配置在它自己目录下的 config/config.json

读写都要容错：配置文件损坏时回退到默认值，而不是让 CLI 起不来。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------


def _config_dir() -> Path:
    """算出配置文件所在目录，优先遵守 XDG 规范。"""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "vct"


CONFIG_PATH = _config_dir() / "config.json"

# 最近打开的文件最多记这么多条
MAX_RECENT_FILES = 10


# --------------------------------------------------------------------------
# 默认值
# --------------------------------------------------------------------------

# 每一项都带中文注释说明用途。键名与交互菜单里的提问一一对应。
DEFAULTS: dict[str, Any] = {
    # ---- 目录记忆 ----
    "last_input": "",            # 上一次处理的输入文件
    "last_output_dir": "",       # 上一次的输出目录
    "recent_files": [],          # 最近处理过的文件列表

    # ---- 语音转字幕 ----
    "asr_engine": "bijian",      # 免费引擎：bijian（必剪）/ jianying（剪映）
    "asr_language": "auto",      # 源语言，auto 表示自动检测

    # ---- 字幕优化与翻译 ----
    "translator": "bing",        # 免费：bing / google
    "target_language": "",       # 空表示不翻译

    # ---- 擦除硬字幕 ----
    "desub_mode": "sttn-auto",   # 默认算法，真人视频效果好且快
    "desub_area": "bottom",      # bottom / full / custom
    "desub_custom_coords": "",   # 自定义区域，格式 "ymin ymax xmin xmax"，多区域用分号隔开

    # ---- 智能镜头分割 ----
    "scene_min_len": 0.6,        # 最短镜头秒数，短于此的会被并进相邻镜头

    # ---- 合成字幕 ----
    "subtitle_mode": "hard",     # hard（烧录）/ soft（嵌入轨道）
    "subtitle_style": "",        # 样式预设名，空表示用默认
    "video_quality": "medium",   # ultra / high / medium / low
}


def _load_raw() -> dict[str, Any]:
    """读取配置文件原始内容。文件不存在或损坏时返回空字典。"""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        # 配置损坏不该阻断用户干活，静默回退默认值
        return {}
    return data if isinstance(data, dict) else {}


# 进程内缓存的配置，避免反复读盘
_cache: dict[str, Any] | None = None


def load() -> dict[str, Any]:
    """读取完整配置（默认值 + 用户覆盖）。"""
    global _cache
    if _cache is None:
        merged = dict(DEFAULTS)
        merged.update(_load_raw())
        _cache = merged
    return _cache


def save() -> bool:
    """把当前配置写回磁盘。

    Returns:
        写入成功返回 True；失败返回 False（调用方自行决定要不要提示）。
    """
    if _cache is None:
        return False
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再改名，避免写到一半崩了导致配置损坏
        temp_path = CONFIG_PATH.with_suffix(".json.tmp")
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(_cache, handle, ensure_ascii=False, indent=2)
        temp_path.replace(CONFIG_PATH)
        return True
    except OSError:
        return False


def get(key: str, default: Any = None) -> Any:
    """读一个配置项。键不存在时返回 default（再否则返回 DEFAULTS 里的值）。"""
    if key in load():
        return load()[key]
    return DEFAULTS.get(key, default)


def set(key: str, value: Any, persist: bool = True) -> None:  # noqa: A001
    """写一个配置项。

    Args:
        key: 配置键。
        value: 新值。
        persist: 是否立即落盘。批量修改时可以传 False，最后统一 save()。
    """
    load()[key] = value
    if persist:
        save()


def update(values: dict[str, Any], persist: bool = True) -> None:
    """批量写配置项。"""
    load().update(values)
    if persist:
        save()


def reset() -> None:
    """恢复默认配置并落盘。"""
    global _cache
    _cache = dict(DEFAULTS)
    save()


# --------------------------------------------------------------------------
# 便捷封装
# --------------------------------------------------------------------------


def remember_file(path: Path | str) -> None:
    """把刚处理过的文件记进最近列表，并更新「上次输入」。

    最近列表去重，最新的排最前面。
    """
    text = str(path)
    recent = [item for item in get("recent_files", []) if item != text]
    recent.insert(0, text)
    update({
        "last_input": text,
        "last_output_dir": str(Path(text).parent),
        "recent_files": recent[:MAX_RECENT_FILES],
    })


def recent_files() -> list[str]:
    """返回最近处理过的、当前仍然存在的文件列表。"""
    return [item for item in get("recent_files", []) if Path(item).exists()]


def preferred_start_dir() -> Path:
    """返回交互菜单里选择文件时的起始目录。

    优先用上次的输出目录，其次是上次输入所在目录，最后是当前工作目录。
    """
    for key in ("last_output_dir", "last_input"):
        value = get(key, "")
        if value:
            path = Path(value)
            directory = path if path.is_dir() else path.parent
            if directory.exists():
                return directory
    return Path.cwd()
