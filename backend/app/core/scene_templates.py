"""镜头分割的预设模板。

这里是模板参数的**唯一真源** —— 前端只负责展示，不维护任何一份副本，
避免两端各写一套阈值然后慢慢漂移。

每个模板就是一组 `vct scene` 的参数预设：
    detector  检测算法          → --detector
    threshold 检测阈值（越小越敏感）→ --threshold（None 表示用 PySceneDetect 默认值）
    min_len   最短镜头秒数       → --min-len
    copy_mode 直接复制流不重编码  → --copy

关于镜头数的说明：
五个模板都在同一条真实素材（`裁剪后-9月16日-05.mp4`，约 2 分钟带货素材）
上做过实测：standard 33 / coarse 14 / fine 38 / handheld 25 / fade 1。
「淡入淡出」只检出 1 个镜头是**检测器特性而非参数错误** —— threshold
检测器只认黑场渐变，普通硬切素材本来就没有黑场，这个模板只对特定片源有用。

DETECTORS 与 DETECTOR_DEFAULT_THRESHOLDS 镜像自 `vctl/commands/scene.py`，
后端不 import vctl（见设计说明：只走命令行契约，不碰它的私有实现）。
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

# 检测器 → 中文标签。镜像 vctl/commands/scene.py 的 DETECTORS。
DETECTORS: Dict[str, str] = {
    "adaptive": "自适应",
    "content": "内容",
    "threshold": "阈值",
}

# 不传 --threshold 时 PySceneDetect 各检测器的默认值，仅用于界面展示。
# 真值由 PySceneDetect 决定，这里不复制一份可能随版本变化的判定逻辑。
DETECTOR_DEFAULT_THRESHOLDS: Dict[str, float] = {
    "adaptive": 3.0,
    "content": 27.0,
    "threshold": 12.0,
}

# 参数合法区间。阈值没有绝对上限，但超过 100 已经不可能检出任何切点。
MIN_THRESHOLD: float = 0.1
MAX_THRESHOLD: float = 100.0
MIN_MIN_LEN: float = 0.1
MAX_MIN_LEN: float = 30.0

# 自定义模板的占位 key。前端选中它时不带具体参数，由用户自己填。
CUSTOM_TEMPLATE_KEY = "custom"

DEFAULT_TEMPLATE_KEY = "standard"


@dataclass(frozen=True)
class SceneTemplate:
    """一个镜头分割预设。"""

    key: str
    name: str
    summary: str
    best_for: str
    detector: str
    threshold: Optional[float]
    min_len: float
    copy_mode: bool = False
    recommended: bool = False

    def to_params(self) -> dict:
        """转成可直接存进 SceneJob.params 的字典。"""
        return {
            "detector": self.detector,
            "threshold": self.threshold,
            "min_len": self.min_len,
            "copy": self.copy_mode,
        }


TEMPLATES: List[SceneTemplate] = [
    SceneTemplate(
        key="standard",
        name="标准切分",
        summary="自适应检测，抗运镜、抗闪光，最短镜头 0.6 秒",
        best_for="口播、带货素材的默认选择。这批素材实测每条 24–53 个镜头",
        detector="adaptive",
        threshold=None,
        min_len=0.6,
        recommended=True,
    ),
    SceneTemplate(
        key="coarse",
        name="快速粗切",
        summary="提高阈值到 35，最短镜头放宽到 2 秒，只切明显的大段落",
        best_for="先摸清素材结构。实测同条素材 14 个镜头（标准切分是 33 个），片段少、切得快",
        detector="content",
        threshold=35.0,
        min_len=2.0,
    ),
    SceneTemplate(
        key="fine",
        name="精细切分",
        summary="降低阈值到 20，最短镜头收到 0.4 秒，保留短镜头和快速转场",
        best_for="快节奏卡点素材。实测同条素材 38 个镜头（标准切分是 33 个），粒度最细，但可能把一次运镜拆成好几段",
        detector="content",
        threshold=20.0,
        min_len=0.4,
    ),
    SceneTemplate(
        key="handheld",
        name="抗运镜",
        summary="自适应检测并把阈值提到 5.0，最短镜头 1 秒",
        best_for="手持、运动镜头多的素材。实测同条素材 25 个镜头（标准切分是 33 个），避免把一次推拉摇移误判成切点",
        detector="adaptive",
        threshold=5.0,
        min_len=1.0,
    ),
    SceneTemplate(
        key="fade",
        name="淡入淡出",
        summary="阈值检测器，专门抓黑场与淡入淡出",
        best_for="只适合靠黑场、淡入淡出分段的片源。注意：普通硬切素材没有黑场，会检出 0 个切点（实测带货素材只有 1 个镜头）",
        detector="threshold",
        threshold=None,
        min_len=0.6,
    ),
]

TEMPLATE_MAP: Dict[str, SceneTemplate] = {item.key: item for item in TEMPLATES}


def get_template(key: str) -> Optional[SceneTemplate]:
    """按 key 取模板，不存在返回 None。"""
    return TEMPLATE_MAP.get(key)


def resolve_params(
    template_key: Optional[str],
    *,
    detector: Optional[str] = None,
    threshold: Optional[float] = None,
    min_len: Optional[float] = None,
    copy_mode: Optional[bool] = None,
) -> dict:
    """把「模板 + 显式覆盖」解析成最终参数。

    选中内置模板时以模板为准，显式传入的字段覆盖模板值（前端切换模板后
    又微调了某个参数就属于这种情况）。模板 key 为空或为 custom 时，
    完全以显式参数为准，缺项回落到标准模板的默认值。

    Returns:
        形如 {"detector": str, "threshold": float | None, "min_len": float, "copy": bool}
    """
    template = get_template(template_key) if template_key else None
    base = template.to_params() if template is not None else SceneTemplate(
        key=CUSTOM_TEMPLATE_KEY,
        name="自定义",
        summary="",
        best_for="",
        detector="adaptive",
        threshold=None,
        min_len=0.6,
    ).to_params()

    if detector is not None:
        base["detector"] = detector
    if threshold is not None:
        base["threshold"] = threshold
    if min_len is not None:
        base["min_len"] = min_len
    if copy_mode is not None:
        base["copy"] = copy_mode

    return base
