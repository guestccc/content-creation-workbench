"""素材目录（materials/）的目录规划。

`materials/` 里的东西一律不入 git（见根目录 `.gitignore`）：原片动辄几百 MB
到几 GB，切分产物更是成倍增长，提交上去会让仓库没法用。但**目录骨架入库** ——
四个分段各带一个 `.gitkeep` 占位，新克隆下来就是规划好的样子。后端启动时再按
这里的定义补建一遍（`ensure_materials_layout()`）：有人删了目录、或者把
`SCENE_MATERIALS_DIR` 指到别处，都不会退化成「啥都往根上堆」的一层。

布局按**流程分段**，每一段只放一类东西：

    materials/
    ├── source/     原始素材：待切的视频往这里拷（镜头分割页的默认输入目录）
    ├── clips/      镜头分割产物：每条原片一个 <视频名>_scenes/ 子目录（默认输出目录）
    ├── subtitle/   字幕提取产物：每条视频一个 <视频名>.srt（字幕提取页的默认输出目录）
    └── output/     成片，待发布

根目录本身不鼓励放东西：原片进 source/，产物进各自的分段目录，
这样「哪些是素材、哪些是产物」一眼可辨，也不会几百个文件糊在一层。

`subtitle/` 是字幕提取页的默认输出目录，页面上可以改到别处；改名规则是
`<视频名>.srt`，重名追加 `-2` / `-3` 后缀（见 services/subtitle_job_service.py）。
"""

from pathlib import Path
from typing import Dict

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# 子目录名做成常量，而不是散落各处的字符串字面量：改名字时不会漏掉某一处。
SOURCE = "source"
CLIPS = "clips"
SUBTITLE = "subtitle"
OUTPUT = "output"

#: 子目录 → 用途说明。顺序即建目录的顺序，也是日志与文档里的顺序。
SUBDIRS: Dict[str, str] = {
    SOURCE: "原始素材：待切的视频往这里拷",
    CLIPS: "镜头分割产物：每条原片一个 <视频名>_scenes/ 子目录",
    SUBTITLE: "字幕提取产物：每条视频一个 <视频名>.srt",
    OUTPUT: "成片，待发布",
}


def materials_root() -> Path:
    """素材目录根路径（支持 ~ 开头，不保证存在）。

    路径解析只写这一处：列目录接口、环境自检、启动建骨架都从这里取，
    避免几处各自 expanduser 出现不一致。
    """
    return Path(settings.SCENE_MATERIALS_DIR).expanduser()


def subdir(name: str) -> Path:
    """素材目录下某个分段的路径（不保证存在）。

    Args:
        name: 分段名，取值见 SOURCE / CLIPS / SUBTITLE / OUTPUT。
    Raises:
        ValueError: 传入了规划之外的分段名 —— 这类错是代码写错，不该静默兜底。
    """
    if name not in SUBDIRS:
        raise ValueError(f"未知的素材子目录：{name}（可选：{', '.join(SUBDIRS)}）")
    return materials_root() / name


def ensure_materials_layout() -> Path:
    """建好素材目录骨架（根目录 + 四个分段），返回可用的根路径。

    应用启动与列目录接口都走这个函数，保证新克隆的仓库一打开就是规划好的样子。

    建不出来时（只读盘、权限不足）退回用户主目录：素材目录只是个便利默认值，
    不该因为它把选目录流程卡死，失败原因记日志备查。
    """
    root = materials_root()
    try:
        for name in SUBDIRS:
            (root / name).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("素材目录不可用，回退到用户主目录：%s（%s）", root, exc)
        return Path.home()
    return root
