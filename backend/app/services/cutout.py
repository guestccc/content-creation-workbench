"""抠图 / 合成：把「深色线条画在浅色背景上」的照片抠成透明底，再贴到另一张图上。

一键换背景的算法核心，移植自 `去掉背景/抠图.py`。纯 numpy + Pillow 的算术流程：
无模型、无权重、无网络，同样的输入跑一万遍结果逐字节相同。

────────────────────────────────────────────────────────────────────────
五步，以及每一步在堵什么漏（这才是真正的技术核心，代码本身很短）：

1) 不透明度 = 亮度从「纸 → 0、墨 → 1」的线性映射。
   **关键是绝不二值化**：笔画边缘的抗锯齿像素本来就是纸与墨的混合，线性映射下
   它们自动拿到 0.3 / 0.6 这类中间透明度，等于白捡了边缘抗锯齿；硬阈值出来的
   边是啃过的，缩放后全是锯齿。

2) 纸纹截断：纸张纤维有 1–8% 的亮度起伏，线性映射会把它们全变成 3–20 的淡灰，
   整张图蒙一层灰。所以映射结果再压一道 (a - 0.08) / 0.92，低端直接归零。

3) 颜色统一成墨色：边缘像素的 RGB 是被白纸「稀释」过的浅色，原样保留的话在白底
   上看没问题，一放到深色背景上就现出一圈**白边**。所以把 RGB 全换成墨芯均值，
   让「颜色」和「透明度」彻底解耦（业内叫 un-premultiply，取常量是它的稳健近似）。

4) 背景不是纯色时（桌面木纹、阴影、纸边），它在亮度上会卡在纸和墨中间 —— 木纹
   亮度 150 上下，纸 235、墨 9，任何全局阈值都会给它 25% 左右的不透明度，整片
   木纹变成一层灰影蒙在画布上。**结论：分类器不可靠时，用「离主体多远」代替
   「像素长什么样」** —— 主体包围盒外整片置零。包围盒本身由「定位线」圈出来，
   而定位线**不能写死一个比例**：主体占画面多大取决于拍得多近。羊1 那张脸在 20%
   档还是 132×124，46% 档就炸成 273×570 —— 写死的 40% 落在炸开之后，「主体」
   成了整幅画面，位置门随即形同虚设。所以默认 dark_frac=None（auto）：扫一遍
   档位，取包围盒炸开（面积涨 1.5 倍）前的那一档。

5) 暖色抑制：木纹有时**就在主体旁边、位置门里面**（羊4：小羊右下方那一片），
   矩形门框不住。但它跟纸、墨在**彩度**上差着一个量级 —— 量出来是纸 0.059、
   墨 0.04、木纹（含浅色木纹）0.13 以上，中间是干净的空档。所以按彩度再压一道：
   彩度超过 0.09 开始压、到 0.15 压到底。旁边那条 dark 是保护条款：亮度低于
   ink+0.35×跨度 的像素一律放行，深色笔迹不会因为带点颜色就被误杀；
   真要用彩色主体，关掉 warm。

阈值不是拍脑袋定的：原脚本的 --check 会打印「不同暗度档位下的包围盒」和「暖色
抑制能抠掉多少像素」，前者定 dark_frac，后者定彩度带。上面那些数字都是从几张
真实照片上量出来的，本模块把它们作为诊断信息一并返回（见 stats），页面上看得见。
────────────────────────────────────────────────────────────────────────

与 `抠图.py` 的三处差别，都是移植时**必须**改的：

- **不抛 SystemExit，改抛 CutoutError**。SystemExit 继承 BaseException，后台
  worker 线程的 `except Exception` 抓不住它，一张过曝的图就会静默杀死整个 worker
  —— 之后所有任务永远停在「排队中」，而健康检查还是绿的。
- **不 print，走 logger**。
- **不读 argparse，参数走 CutoutParams**（由任务表的 params JSON 白名单键构造）。

适用范围（刻意收窄）：**只对「浅色纸/浅底 + 深色线条」有效**。手绘涂鸦、白板字、
线稿照片都是它的主场；普通照片（人像、商品）不适用 —— 那种要靠分割模型，不是
这套算术能解决的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from app.core.logging import get_logger

logger = get_logger(__name__)

#: 亮度权重（Rec.601）
LUMA_WEIGHTS = np.array([0.299, 0.587, 0.114], dtype=np.float32)

#: 自动选定位线时扫的档位（比诊断用的密，断崖才找得准）
DARK_STEPS = (0.10, 0.14, 0.18, 0.22, 0.26, 0.30, 0.34, 0.38, 0.42, 0.46, 0.50, 0.56)
#: 包围盒面积较上一档涨这么多 = 炸开
DARK_JUMP = 1.5
#: 没扫出断崖时的兜底档位
DARK_DEFAULT = 0.40

#: 暖色抑制：彩度高于 C_LO 开始压、到 C_HI 压到底；亮度低于 GUARD_LO 的一律放行
#: 0.09/0.15 是量出来的：纸 0.059、墨 0.04，木纹（含浅色木纹）0.13 往上，鸿沟很干净
WARM_C_LO, WARM_C_HI = 0.09, 0.15
WARM_GUARD_LO, WARM_GUARD_HI = 0.35, 0.55

#: 贴纸与背景的亮度跨度低于这个值就提醒「主体和背景太近了」
MIN_SPAN_HINT = 40.0


class CutoutError(ValueError):
    """这张图抠不出来 / 贴不上。

    继承 ValueError 而不是 SystemExit：后台 worker 线程只捕 Exception，
    SystemExit 会直接穿出去把工作线程弄死（见模块 docstring）。
    """


@dataclass
class CutoutParams:
    """一套抠图 + 贴合参数。默认值等于 `抠图.py` 的命令行默认值，**只有 scale 例外**
    （脚本默认 None = 原尺寸，这里默认 0.1，理由见下面那一行的注释）。

    前六个是抠图本身的（对应原脚本的 --hi-frac / --lo-frac / --dark-frac /
    --pedestal / --no-gate / --gate-pad / --no-warm-filter / --keep-color），
    后六个是贴到背景上时的（对应 --scale / --pos / --search-from / --margin /
    --rotate / --opacity）。
    """

    # ---- 抠图 ----
    hi_frac: float = 0.90          # 纸的透明线（相对纸–墨跨度）
    lo_frac: float = 0.15          # 墨的实心线
    dark_frac: Optional[float] = None   # 主体定位线档位；None = auto（按断崖自动选）
    pedestal: float = 0.08         # 纸纹截断
    gate: bool = True              # 位置门：主体包围盒外整片置零
    gate_pad: float = 0.12         # 位置门往外扩的比例
    warm: bool = True              # 暖色抑制
    keep_color: bool = False       # 保留原始像素颜色（默认统一成墨色）

    # ---- 贴合 ----
    # 贴纸宽度占底图宽度的比例；None = 不缩放（按原尺寸居中贴）
    # 默认 0.1 = 占底图宽的 10%，是**刻意偏离脚本默认**的一处：手机拍的手绘动辄
    # 1080×1440，随手挑的背景常常比它小，原尺寸贴会直接判失败（见 composite）。
    # 给个放得下的默认值，不改参数就能出一批能看的图；要原尺寸就显式传 None。
    scale: Optional[float] = 0.1
    pos: Optional[str] = None      # center（默认）/ auto / tl / tr / bl / br / "x,y"
    search_from: float = 0.35      # 自动落点的搜索起点（页高比例），pos=auto 时生效
    margin: int = 20               # 贴纸离底图边缘的最小距离
    rotate: float = 0.0            # 贴纸旋转角度（度）
    opacity: float = 1.0           # 贴纸不透明度 0–1

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "CutoutParams":
        """从任务表的 params JSON 构造。

        只认本类的字段（白名单），多余键直接丢掉 —— 任务记录里的 JSON 是历史数据，
        老任务存过的形状不该让新代码崩。类型不合法时退回默认值，不抛异常：
        参数校验是 schema 层的职责，这里只做防御性兜底。
        """
        if not data:
            return cls()

        kwargs: dict[str, Any] = {}
        for name, spec in cls.__dataclass_fields__.items():
            if name not in data:
                continue
            value = data[name]
            if value is None:
                # scale / pos / dark_frac 的 None 是有意义的值（= 用默认行为），保留
                kwargs[name] = None
                continue
            try:
                if spec.type is bool:
                    kwargs[name] = bool(value)
                elif name == "scale" or name == "dark_frac":
                    kwargs[name] = float(value)
                elif name in ("hi_frac", "lo_frac", "pedestal", "gate_pad",
                              "search_from", "rotate", "opacity"):
                    kwargs[name] = float(value)
                elif name == "margin":
                    kwargs[name] = int(value)
                elif name == "pos":
                    kwargs[name] = str(value)
                elif name == "keep_color":
                    kwargs[name] = bool(value)
            except (TypeError, ValueError):
                logger.warning("换背景参数 %s 的值不合法，已退回默认：%r", name, value)
        return cls(**kwargs)

    def as_dict(self) -> dict:
        """回写任务表用的纯数据形式（JSON 列要能直接存）。"""
        return {
            "hi_frac": self.hi_frac,
            "lo_frac": self.lo_frac,
            "dark_frac": self.dark_frac,
            "pedestal": self.pedestal,
            "gate": self.gate,
            "gate_pad": self.gate_pad,
            "warm": self.warm,
            "keep_color": self.keep_color,
            "scale": self.scale,
            "pos": self.pos,
            "search_from": self.search_from,
            "margin": self.margin,
            "rotate": self.rotate,
            "opacity": self.opacity,
        }


# ---------------------------------------------------------------------------
# 读图
# ---------------------------------------------------------------------------

def check_pixel_count(size: tuple[int, int], max_pixels: int, *, what: str) -> None:
    """像素闸门：超限的图当场拒掉，别等算到一半把内存吃爆。

    这个算法跑在 worker 线程里，**没有子进程隔离** —— 一张 48MP 的照片能吃到
    1.5–2GB，把整个后端连同另外几个 worker 一起带走。所以在 `Image.open()` 之后、
    真正解码之前就按 `im.size` 判掉（读 size 不解码，几乎零成本）。

    Raises:
        CutoutError: 像素数超过上限。
    """
    width, height = size
    pixels = width * height
    if pixels > max_pixels:
        # 除以 1e4 而不是 1e6：这里报的是「万像素」，手机照片常见口径（4800 万像素）。
        # 按 1e6 除再标「万」会差 100 倍 —— 用户看到的是「你的图超过上限 30 万像素」，
        # 而实际上限是 3000 万，一张正常的图会被这句话说成早就该缩放了。
        raise CutoutError(
            f"{what}是 {width}×{height}（{pixels / 10_000:.0f} 万像素），"
            f"超过单张上限 {max_pixels / 10_000:.0f} 万像素；请先缩放再处理"
        )


def load_rgb(path: Path, *, max_pixels: int) -> np.ndarray:
    """读图并**转正**：手机照片带 EXIF 方向标记，不转的话会躺倒。

    用 `with` 而不是裸 `Image.open`：open 是惰性的，不关会攒着 fd（一批 200 张
    撞上 macOS 默认 256 的软限就是 `Too many open files`）。
    """
    if not path.is_file():
        raise CutoutError(f"找不到原图：{path}")
    try:
        with Image.open(path) as im:
            check_pixel_count(im.size, max_pixels, what="原图")
            return np.asarray(ImageOps.exif_transpose(im).convert("RGB"))
    except CutoutError:
        raise
    except OSError as exc:
        raise CutoutError(f"读不出这张图（{exc}）") from exc


def load_page(path: Path, *, max_pixels: int) -> Image.Image:
    """读背景图（贴纸要盖上去的那张）。"""
    if not path.is_file():
        raise CutoutError(f"找不到背景图：{path}")
    try:
        with Image.open(path) as im:
            check_pixel_count(im.size, max_pixels, what="背景图")
            return ImageOps.exif_transpose(im).convert("RGB")
    except CutoutError:
        raise
    except OSError as exc:
        raise CutoutError(f"读不出这张背景图（{exc}）") from exc


def luma_of(rgb: np.ndarray) -> np.ndarray:
    return rgb @ LUMA_WEIGHTS


def chroma_of(rgb: np.ndarray) -> np.ndarray:
    """彩度 0–1。木纹这类暖色背景靠它跟灰白的纸区分。"""
    return (rgb.max(axis=2) - rgb.min(axis=2)) / 255.0


def bbox_of(mask: np.ndarray) -> Optional[tuple[int, int, int, int]]:
    ys, xs = np.where(mask)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def fmt_bbox(bb: Optional[tuple[int, int, int, int]]) -> str:
    return "—" if bb is None else (
        f"({bb[0]},{bb[1]})-({bb[2]},{bb[3]}) {bb[2] - bb[0] + 1}×{bb[3] - bb[1] + 1}"
    )


def dark_tail_mean(lum: np.ndarray, frac: float = 0.001, floor: int = 500) -> float:
    """墨色基线 = 最暗那一小撮像素的**均值**。

    别用分位数：羊1 那张图上墨芯（<60）只占 0.305%，而 0.5% 分位正好落在笔画边
    缘上，量出来的「墨」是 131 而不是 9 —— 基线一错，后面透明线/实心线/定位线
    全跟着跑偏，主体包围盒直接变成整幅画面。取一小撮的均值稳得多，它量的是
    「最黑的那一撮究竟有多黑」，不受主体占比影响。
    """
    k = min(lum.size, max(floor, int(frac * lum.size)))
    return float(np.partition(lum.ravel(), k - 1)[:k].mean())


def auto_dark_frac(lum: np.ndarray, ink: float, span: float) -> tuple[float, str]:
    """自动挑「主体定位线」：包围盒在某一档突然变大 = 那档已经把背景吃进来了，取它前一档。

    写死一个比例是不行的：主体在画面里占多大，取决于拍得多近、画得多小。
    羊1 那张脸在 20% 档还是 132×124，46% 档就炸成 273×570 —— 默认的 40% 落在
    炸开之后，「主体」成了整幅画面，位置门随即形同虚设，木纹全漏进来。
    而羊3 一直稳到 46% 才炸，同一张图用 40% 和用 46% 结果一样。所以按图上真实的
    断崖来选。
    """
    prev: Optional[tuple[float, int]] = None
    for frac in DARK_STEPS:
        bb = bbox_of(lum < ink + frac * span)
        if bb is None:
            continue
        area = (bb[2] - bb[0] + 1) * (bb[3] - bb[1] + 1)
        if prev is not None and area > DARK_JUMP * prev[1]:
            note = f"（{prev[0]:.2f} 档 {prev[1]}px² → {frac:.2f} 档 {area}px²，炸开前那一档）"
            return prev[0], note
        prev = (frac, area)
    if prev is None:
        return DARK_DEFAULT, "（没扫出断崖，用兜底值）"
    return prev[0], "（没扫出断崖，取能扫到的最暗档）"


class Levels:
    """纸与墨的亮度基线，以及由它派生的三个阈值。

    墨取最暗一小撮的均值（见上），纸取 90 分位（用最值会被反光点带跑）。
    定位线（dark_frac）传 None 表示 auto —— 自己找断崖。
    """

    def __init__(self, lum: np.ndarray, hi_frac: float, lo_frac: float,
                 dark_frac: Optional[float]):
        self.ink = dark_tail_mean(lum)                     # 墨芯亮度
        self.paper = float(np.percentile(lum, 90))         # 纸面亮度
        self.span = self.paper - self.ink
        self.hi = self.ink + hi_frac * self.span           # 到这亮度就全透明
        self.lo = self.ink + lo_frac * self.span           # 到这亮度就全不透明
        self.auto = dark_frac is None
        if self.auto:
            self.dark_frac, self.dark_note = auto_dark_frac(lum, self.ink, self.span)
        else:
            self.dark_frac = float(dark_frac)
            self.dark_note = f"（手动指定 {self.dark_frac:.2f}）"
        self.dark = self.ink + self.dark_frac * self.span  # 拿它圈出主体所在的矩形

    def describe(self) -> str:
        return (f"墨 {self.ink:.1f} / 纸 {self.paper:.1f}（跨度 {self.span:.1f}）→ "
                f"透明线 {self.hi:.1f}、实心线 {self.lo:.1f}、"
                f"定位线 {self.dark:.1f}（{self.dark_frac:.0%}）{self.dark_note}")


# ---------------------------------------------------------------------------
# 抠图
# ---------------------------------------------------------------------------

def build_alpha(lum: np.ndarray, lv: Levels, pedestal: float) -> np.ndarray:
    """亮度 → 不透明度：第 1 步的线性映射 + 第 2 步的纸纹截断。"""
    alpha = np.clip((lv.hi - lum) / max(lv.hi - lv.lo, 1e-6), 0.0, 1.0)
    # 低端截断：不平移的话纸纹会留下 1–8% 的灰，整张图蒙雾
    return np.clip((alpha - pedestal) / (1.0 - pedestal), 0.0, 1.0)


def gate_mask(shape: tuple[int, int], bb: tuple[int, int, int, int], pad: int) -> np.ndarray:
    """第 4 步：主体包围盒往外扩 pad 像素，框外全部置零。"""
    h, w = shape
    gate = np.zeros((h, w), dtype=np.float32)
    x0, y0, x1, y1 = bb
    gate[max(0, y0 - pad):min(h, y1 + 1 + pad),
         max(0, x0 - pad):min(w, x1 + 1 + pad)] = 1.0
    return gate


def warm_weight(lum: np.ndarray, rgb: np.ndarray, lv: Levels) -> np.ndarray:
    """暖色抑制：木纹/桌面/暖色阴影这类像素，亮度映射和位置门都挡不住它 ——

    · 亮度映射挡不住：它落在纸和墨中间，天然拿到 20% 左右的不透明度，白纸上就是一层灰；
    · 位置门挡不住：它在门框**里面**（羊4 的木纹就在小羊右下方、门框范围内的那片）。

    但它的彩度跟纸和墨差着一个量级（木纹 0.25，纸 0.06，墨 0.04），按彩度抠掉。
    亮度低于 guard_lo 的像素一律放行（dark=1），所以彩色笔迹只要够深就不会误杀。
    """
    ch = chroma_of(rgb)
    warm = np.clip((ch - WARM_C_LO) / (WARM_C_HI - WARM_C_LO), 0.0, 1.0)
    # 越暗 → dark 越接近 1 → 越不受暖色抑制影响
    dark = np.clip((lv.ink + WARM_GUARD_HI * lv.span - lum)
                   / max(1.0, (WARM_GUARD_HI - WARM_GUARD_LO) * lv.span), 0.0, 1.0)
    return 1.0 - warm * (1.0 - dark)


def ink_color(rgb: np.ndarray, lum: np.ndarray, lv: Levels) -> np.ndarray:
    """墨芯的平均颜色：只取最暗那一撮像素，别把边缘的被稀释色算进去。"""
    core = rgb[lum < lv.ink + 0.20 * lv.span]
    if core.size == 0:
        core = rgb[lum < lv.lo]
    if core.size == 0:                      # 整张图比 lo 还亮：退到最暗的那一撮
        core = rgb.reshape(-1, 3)[np.argsort(lum.ravel())[: max(1, lum.size // 1000)]]
    return np.clip(np.round(core.mean(axis=0)), 0, 255).astype(np.uint8)


def cutout(rgb: np.ndarray, params: CutoutParams) -> tuple[np.ndarray, dict]:
    """抠图：返回 (RGBA uint8 数组, 诊断信息)。

    整个过程是纯算术，没有任何模型。

    Raises:
        CutoutError: 图里没有比背景暗的主体（过曝、纯渐变、空白纸）。
    """
    h, w = rgb.shape[:2]
    lum = luma_of(rgb)
    lv = Levels(lum, params.hi_frac, params.lo_frac, params.dark_frac)
    warnings: list[str] = []
    if lv.span < MIN_SPAN_HINT:
        warnings.append(
            f"主体与背景亮度只差 {lv.span:.0f} 档，太近了，抠出来恐怕不干净；"
            "换张对比更分明的照片，或把透明线/实心线手动压窄"
        )
        logger.warning("换背景：主体与背景亮度只差 %.0f 档，结果可能不干净", lv.span)

    alpha = build_alpha(lum, lv, params.pedestal)

    # 主体定位 + 位置门（第 4 步）
    bb = bbox_of(lum < lv.dark)
    if bb is None:
        raise CutoutError("这张图里没有比背景暗的主体（没有深色线条？）")
    if (bb[2] - bb[0] + 1) * (bb[3] - bb[1] + 1) > 0.5 * w * h:
        warnings.append(
            f"定位线（亮度 {lv.dark:.0f}）圈出来的主体占了画面一半以上，"
            "多半把背景也圈进来了，位置门挡不住它；把定位线档位改到包围盒炸开之前"
        )
        logger.warning("换背景：定位线圈出的主体占画面一半以上，位置门可能失效")
    pad = max(12, int(params.gate_pad * max(bb[2] - bb[0], bb[3] - bb[1])))
    if params.gate:
        alpha = alpha * gate_mask((h, w), bb, pad)

    warm_cut = 0
    if params.warm:
        keep = warm_weight(lum, rgb, lv)   # 变量名别叫 w —— 上面 w 是画布宽度
        warm_cut = int(((alpha > 0.02) & (keep < 0.5)).sum())
        alpha = alpha * keep

    ink = ink_color(rgb, lum, lv)
    out = np.empty((h, w, 4), dtype=np.uint8)
    # 第 3 步：颜色统一。keep_color 保留原色（想要彩色主体时用，但深色底上会有白边）
    if params.keep_color:
        out[..., :3] = rgb.astype(np.uint8)
    else:
        out[..., 0], out[..., 1], out[..., 2] = ink
    out[..., 3] = np.round(alpha * 255).astype(np.uint8)

    visible = int((out[..., 3] > 8).sum())
    stats = {
        "paper": round(lv.paper, 1),
        "ink_luma": round(lv.ink, 1),
        "span": round(lv.span, 1),
        "hi": round(lv.hi, 1),
        "lo": round(lv.lo, 1),
        "dark": round(lv.dark, 1),
        "dark_frac": round(lv.dark_frac, 4),
        "dark_auto": lv.auto,
        "dark_note": lv.dark_note,
        "dark_line": lv.describe(),
        "bbox": list(bb),
        "bbox_text": fmt_bbox(bb),
        "gate": params.gate,
        "gate_pad": pad if params.gate else 0,
        "warm": params.warm,
        "warm_cut": warm_cut,
        "ink_rgb": [int(v) for v in ink],
        "canvas": [w, h],
        "opaque_px": int((out[..., 3] > 200).sum()),
        "visible_px": visible,
        "visible_ratio": round(visible / (w * h), 6),
        "warnings": warnings,
    }
    return out, stats


# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------

def energy_map(page: Image.Image) -> np.ndarray:
    """字迹密度：原图与高斯模糊之差。空白处 ≈ 0，字越密越大。"""
    grey = page.convert("L")
    g = np.asarray(grey, dtype=np.float32)
    blur = np.asarray(grey.filter(ImageFilter.GaussianBlur(3)), dtype=np.float32)
    return np.abs(g - blur)


def integral(arr: np.ndarray) -> np.ndarray:
    """二维前缀和（积分图）：让任意矩形内的和 O(1) 查出来。

    朴素做法在每个候选位置重算一遍框内和，是 O(宽×高×框宽×框高) ≈ 十亿次；
    积分图把它压成 O(宽×高)。
    """
    return np.pad(arr.cumsum(0).cumsum(1), ((1, 0), (1, 0)))


def box_sum(ii: np.ndarray, x: int, y: int, w: int, h: int) -> float:
    return float(ii[y + h, x + w] - ii[y, x + w] - ii[y + h, x] + ii[y, x])


def auto_position(page: Image.Image, size: tuple[int, int],
                  search_from: float, margin: int) -> tuple[int, int]:
    """找页面上最空的落点：让贴纸在候选区里滑一遍，取覆盖处字迹总量最小的位置。"""
    pw, ph = page.size
    w, h = size
    ii = integral(energy_map(page))
    best: Optional[tuple[float, int, int]] = None
    for y in range(int(ph * search_from), max(int(ph * search_from) + 1, ph - h - margin), 4):
        for x in range(margin, max(margin + 1, pw - w - margin), 4):
            score = box_sum(ii, x, y, w, h)
            if best is None or score < best[0]:
                best = (score, x, y)
    if best is None:
        return (pw - w) // 2, (ph - h) // 2
    return best[1], best[2]


def resolve_position(spec: str, page: Image.Image, size: tuple[int, int],
                     search_from: float, margin: int) -> tuple[int, int]:
    """把位置规格解析成左上角坐标。规格：center / auto / tl / tr / bl / br / "x,y"。"""
    pw, ph = page.size
    w, h = size
    if spec == "auto":
        return auto_position(page, size, search_from, margin)
    if spec == "center":
        return (pw - w) // 2, (ph - h) // 2
    corners = {
        "tl": (margin, margin),
        "tr": (pw - w - margin, margin),
        "bl": (margin, ph - h - margin),
        "br": (pw - w - margin, ph - h - margin),
    }
    if spec in corners:
        return corners[spec]
    try:
        x, y = (int(v) for v in spec.split(","))
        return x, y
    except ValueError:
        raise CutoutError(f"不认「{spec}」这个位置：用 center / auto / tl / tr / bl / br / x,y")


def trim_to_alpha(rgba: np.ndarray, pad: int = 6) -> Image.Image:
    """裁到不透明区域的包围盒（留一点抗锯齿边），别把整幅透明画布当贴纸。"""
    image = Image.fromarray(rgba, "RGBA")
    bb = bbox_of(rgba[..., 3] > 8)
    if bb is None:
        raise CutoutError("这张贴纸整幅都是透明的，没东西可贴")
    x0, y0, x1, y1 = bb
    return image.crop((max(0, x0 - pad), max(0, y0 - pad),
                       min(image.width, x1 + 1 + pad), min(image.height, y1 + 1 + pad)))


def composite(sticker: Image.Image, page: Image.Image,
              params: CutoutParams) -> tuple[Image.Image, tuple[int, int]]:
    """把抠好的图贴到背景上，返回 (成图, 落点)。

    scale=None（脚本默认）时保持原尺寸、原画布，不裁边不缩放 —— 主体在画布里的
    位置跟它在原图里一模一样，然后整体居中盖在背景图上；给了 scale 就裁到主体
    包围盒再缩到「底图宽 × scale」，默认的 0.1 走的就是这条路。

    与脚本的一处刻意差别：贴纸比底图大时，脚本只打印一句警告然后照样裁掉 ——
    批量场景下这是**静默降质**。这里改成直接报错，让用户去调缩放或换背景。
    """
    st = sticker
    if params.scale is not None:
        # 有 scale 就裁边 + 缩放到目标宽度（贴纸宽 = 底图宽 × scale）
        st = trim_to_alpha(np.asarray(sticker))
        target_w = max(1, int(page.width * params.scale))
        st = st.resize((target_w, max(1, round(st.height * target_w / st.width))), Image.LANCZOS)
    if params.rotate:
        st = st.rotate(params.rotate, expand=True, resample=Image.BICUBIC)

    if st.width > page.width or st.height > page.height:
        raise CutoutError(
            f"抠出的图 {st.width}×{st.height} 比背景 {page.width}×{page.height} 大，"
            "贴上去会被裁掉；请设置缩放比例，或换一张更大的背景"
        )

    # 默认居中；pos=auto 时才去自动找空白落点
    spec = params.pos or "center"
    pos = resolve_position(spec, page, st.size, params.search_from, params.margin)
    if params.opacity < 1.0:
        arr = np.asarray(st).copy()
        arr[..., 3] = np.round(arr[..., 3] * params.opacity).astype(np.uint8)
        st = Image.fromarray(arr, "RGBA")

    out = page.convert("RGB").copy()
    out.paste(st, pos, st)          # 带 mask 粘贴 = 标准 alpha 合成 out = 底×(1-a) + 墨×a
    return out, pos


# ---------------------------------------------------------------------------
# 一张图的完整流程
# ---------------------------------------------------------------------------

def sticker_from(path: Path, params: CutoutParams, *, max_pixels: int) -> tuple[Image.Image, dict]:
    """贴纸来源：已经是透明底就直接用，否则先按同一套流程抠一遍。

    这条分支不能省：对已经是透明底的图再抠一遍，会拿透明区的 RGB（通常是均匀的
    墨色）当输入，量出来的「纸」和「墨」一样黑，整幅画布都会被判成主体。
    """
    if not path.is_file():
        raise CutoutError(f"找不到原图：{path}")
    try:
        with Image.open(path) as im:
            check_pixel_count(im.size, max_pixels, what="原图")
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA") or "transparency" in im.info:
                rgba = im.convert("RGBA")
                # 判据是「有不透明以下的像素」= alpha 真的被用上了。写成 max() < 255
                # 就反了：抠好的图里笔画是实心 255，那样判会把已经是透明底的图又抠一遍
                if int(np.asarray(rgba)[..., 3].min()) < 255:
                    return rgba, {
                        "already_transparent": True,
                        "canvas": [rgba.width, rgba.height],
                        "visible_px": int((np.asarray(rgba)[..., 3] > 8).sum()),
                        "warnings": [],
                    }
    except CutoutError:
        raise
    except OSError as exc:
        raise CutoutError(f"读不出这张图（{exc}）") from exc

    logger.info("换背景：「%s」没有透明通道，按抠图流程处理一遍", path.name)
    rgba, stats = cutout(load_rgb(path, max_pixels=max_pixels), params)
    stats["already_transparent"] = False
    return Image.fromarray(rgba, "RGBA"), stats


def render_one(source: Path, page: Image.Image, params: CutoutParams, *,
               max_pixels: int) -> tuple[Image.Image, dict]:
    """一张原图 → 换好背景的整图 + 诊断统计。

    Raises:
        CutoutError: 这张图抠不出来或贴不上（调用方按「这一张失败」处理，不该中断整批）。
    """
    sticker, stats = sticker_from(source, params, max_pixels=max_pixels)
    out, pos = composite(sticker, page, params)
    stats.update({
        "page": [page.width, page.height],
        "output": [out.width, out.height],
        "position": [pos[0], pos[1]],
        "scale": params.scale,
        "sticker": [sticker.width, sticker.height],
    })
    return out, stats


def diagnostic_report(rgb: np.ndarray, params: CutoutParams) -> str:
    """`抠图.py --check` 的等价物：把「能不能抠干净」的统计打成一段文本。

    调参时看这个 —— 它回答的是「定位线该定在哪一档」「框外那圈干不干净」，
    而这些光看产出的 PNG 是看不出来的。目前只供命令行排查用，接口不暴露。
    """
    h, w = rgb.shape[:2]
    lum, chroma = luma_of(rgb), chroma_of(rgb)
    lv = Levels(lum, params.hi_frac, params.lo_frac, params.dark_frac)
    lines = [
        f"尺寸 {w}×{h}，共 {w * h} 像素",
        "亮度分位：" + "  ".join(
            f"{p}%={np.percentile(lum, p):.0f}" for p in (0.5, 5, 25, 50, 75, 90, 99)),
        f"亮度基线：{lv.describe()}",
        f"彩度分位：5%={np.percentile(chroma, 5):.3f}  50%={np.percentile(chroma, 50):.3f}  "
        f"95%={np.percentile(chroma, 95):.3f}（背景有木纹这类暖色时 95% 会明显偏高）",
        "",
        "不同暗度档位圈出来的主体 —— 包围盒突然变大 = 那档已经把背景吃进来了：",
    ]
    steps = sorted(set((0.20, 0.30, 0.40, 0.50, 0.60, 0.75)) | {round(lv.dark_frac, 2)})
    for frac in steps:
        thr = lv.ink + frac * lv.span
        mask = lum < thr
        n = int(mask.sum())
        mark = "  ← 当前定位线" if abs(frac - lv.dark_frac) < 1e-6 else ""
        lines.append(
            f"  暗于 {thr:5.1f}（跨度 {frac:>4.0%}）：{n:>8} 像素（占 {n / (w * h):>6.2%}）"
            f"  bbox={fmt_bbox(bbox_of(mask))}{mark}"
        )
    return "\n".join(lines)
