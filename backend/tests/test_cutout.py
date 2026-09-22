"""抠图 / 合成算法（services/cutout.py）测试。

**全部用 numpy 合成图**，不读真实照片：这里要钉的是「这一步的算术对不对」，
而不是「某张样例图跑出来好不好看」—— 后者属于调参，前者才是测试的事。
合成图能精确算出每个像素该拿多少透明度，断言可以写死。

覆盖的是五步里每一步在堵的那个漏，以及三个会直接改变任务结果的边界：
- 过曝图必须抛 CutoutError（**不能是 SystemExit** —— 那会静默杀死 worker 线程，
  一批 200 张里只要有一张过曝就会命中）；
- 已经是透明底的输入不重抠（重抠会拿透明区的 RGB 当输入，整幅画布被判成主体）；
- 贴纸比背景大时直接报错（脚本原版只打印警告然后裁掉，批量场景下是静默降质）。
"""

import numpy as np
import pytest
from PIL import Image

from app.services.cutout import (
    CutoutError,
    CutoutParams,
    check_pixel_count,
    composite,
    cutout,
    load_page,
    render_one,
    sticker_from,
)

PAPER = 240   # 纸面亮度
INK = 10      # 墨芯亮度


def _synthetic(*, gate_patch: bool = False, speck: bool = False) -> np.ndarray:
    """造一张「浅纸 + 中央深色方块」的合成图，可选地加两块干扰。

    Args:
        gate_patch: 在**右上角**放一块 180 的灰块（比纸暗、比墨亮，且高于定位线
            138.8），用来验证位置门把它整片置零。
        speck: 在主体包围盒内、主体之外放一块 208 的贴纸，用来验证纸纹截断
            （它的线性透明度只有 0.05，不截断的话会留下一层淡灰）。

    Returns:
        (200, 300, 3) 的 uint8 RGB 数组。主体是 80:160 行 × 120:220 列。
    """
    rgb = np.full((200, 300, 3), PAPER, dtype=np.uint8)
    rgb[80:160, 120:220] = INK
    if gate_patch:
        rgb[5:30, 270:295] = 180
    if speck:
        # 落在位置门内（门是 68:172 行 × 108:232 列）、但在主体之外
        rgb[70:75, 115:120] = 208
    return rgb


# --------------------------------------------------------------------------
# 五步各自在堵的漏
# --------------------------------------------------------------------------


class TestAlpha:
    """第 1、2 步：线性映射 + 纸纹截断。"""

    def test_ink_is_opaque_and_paper_is_clear(self):
        """墨芯拿到 255、纸面拿到 0 —— 这是整套流程的地基。"""
        rgba, stats = cutout(_synthetic(), CutoutParams())
        alpha = rgba[..., 3]
        assert alpha[120, 170] == 255        # 主体正中
        assert alpha[5, 5] == 0              # 远离主体的纸面
        assert stats["ink_luma"] == pytest.approx(INK, abs=0.5)
        assert stats["paper"] == pytest.approx(PAPER, abs=0.5)

    def test_alpha_is_never_binarized(self):
        """边缘像素拿到中间透明度（绝不二值化）。

        合成图上造不出抗锯齿边，但可以直接验「中间亮度的像素拿到中间透明度」：
        这是第 1 步的全部意义 —— 二值化出来的边在缩放后全是锯齿。
        """
        rgb = _synthetic()
        # 把主体最上一行改成介于纸墨之间的灰（跨度 230，取 0.5 → 125）
        rgb[80, 120:220] = 125
        rgba, _ = cutout(rgb, CutoutParams())
        alpha = rgba[80, 170, 3]
        assert 0 < alpha < 255

    def test_pedestal_zeroes_faint_paper_texture(self):
        """第 2 步：线性透明度低于纸纹截断的淡灰直接归零。

        208 这个亮度算出来的线性透明度是 0.05，默认 pedestal=0.08 把它压成 0；
        把 pedestal 关掉，同一块像素就会留下一层灰 —— 那正是「整张蒙雾」的来源。
        """
        rgb = _synthetic(speck=True)
        with_pedestal, _ = cutout(rgb, CutoutParams())
        without, _ = cutout(rgb, CutoutParams(pedestal=0.0))
        assert with_pedestal[72, 117, 3] == 0
        assert without[72, 117, 3] > 0


class TestColorUnify:
    """第 3 步：颜色统一成墨芯均值。"""

    def test_rgb_is_replaced_by_ink_core_mean(self):
        """不统一的话，边缘那些被白纸稀释过的浅色会在深色背景上现出一圈白边。"""
        rgba, stats = cutout(_synthetic(), CutoutParams())
        assert stats["ink_rgb"] == [INK, INK, INK]
        # 主体正中：整幅图只剩墨色和透明度两个自由度
        assert list(rgba[120, 170, :3]) == [INK, INK, INK]

    def test_keep_color_keeps_original_pixels(self):
        """keep_color 保留原像素颜色（要贴彩色主体时用，代价是深底上有白边）。"""
        rgb = _synthetic()
        rgb[80:160, 120:220] = (200, 30, 30)   # 一个红色主体
        rgb[80:160, 120:220][:, :, 2] = 30
        rgba, _ = cutout(rgb, CutoutParams(keep_color=True))
        assert list(rgba[120, 170, :3]) == [200, 30, 30]


class TestGate:
    """第 4 步：位置门 —— 主体包围盒之外整片置零。"""

    def test_patch_outside_subject_is_zeroed_by_gate(self):
        """右上角那块灰（背景不是纯色时的典型情况）必须被整片抹掉。

        它的亮度落在纸墨之间，任何全局阈值都会给它一部分透明度 —— 用「离主体多远」
        代替「像素长什么样」才是可靠的判据。
        """
        rgb = _synthetic(gate_patch=True)
        gated, _ = cutout(rgb, CutoutParams())
        ungated, _ = cutout(rgb, CutoutParams(gate=False))
        assert ungated[15, 280, 3] > 0        # 不关门时它确实会显出来
        assert gated[15, 280, 3] == 0         # 关上门就整片归零

    def test_gate_keeps_the_subject(self):
        """门不能把主体一起关掉 —— 扩边像素是给抗锯齿留的。"""
        rgba, _ = cutout(_synthetic(), CutoutParams())
        assert rgba[120, 170, 3] == 255

    def test_gate_pad_controls_the_margin(self):
        """扩边越大，主体框外保留的像素越多。"""
        rgb = _synthetic()
        # 主体右侧 5 像素处放一块够暗（100）的像素：定位线 138.8 会把它算进包围盒，
        # 所以这里换个思路 —— 直接比不同 pad 下警告之外的可见像素数
        tight, _ = cutout(rgb, CutoutParams(gate_pad=0.0))
        loose, _ = cutout(rgb, CutoutParams(gate_pad=0.5))
        assert (loose[..., 3] > 0).sum() >= (tight[..., 3] > 0).sum()


class TestWarm:
    """第 5 步：暖色抑制 —— 主体旁边、门框之内的木纹。"""

    def test_warm_pixel_next_to_subject_is_suppressed(self):
        """暖色像素跟纸、墨在彩度上差一个量级，按彩度抠掉（位置门框不住它）。"""
        rgb = _synthetic()
        # 在**包围盒内、主体之外**放一块暖色（木纹）：色值 (170, 140, 90) 的彩度很高
        rgb[70:78, 115:125] = (170, 140, 90)

        filtered, _ = cutout(rgb, CutoutParams(warm=True))
        kept, _ = cutout(rgb, CutoutParams(warm=False))
        assert kept[73, 118, 3] > 0
        assert filtered[73, 118, 3] == 0

    def test_dark_ink_with_a_color_is_not_killed(self):
        """保护条款：够深的彩色笔迹不能被当成暖色背景误杀。"""
        rgb = _synthetic()
        rgb[80:160, 120:220] = (40, 10, 10)    # 深红笔迹，彩度不低但足够暗
        rgba, _ = cutout(rgb, CutoutParams(warm=True))
        assert rgba[120, 170, 3] > 200


class TestDiagnostics:
    """--check 那套诊断数字要一并返回：这个算法的失败模式光看产物图判不出来。"""

    def test_stats_carry_the_numbers_needed_to_debug(self):
        _, stats = cutout(_synthetic(), CutoutParams())
        for key in (
            "paper", "ink_luma", "span", "dark", "dark_frac", "dark_note",
            "dark_line", "bbox", "bbox_text", "gate", "gate_pad", "warm",
            "warm_cut", "ink_rgb", "canvas", "opaque_px", "visible_px",
            "visible_ratio", "warnings",
        ):
            assert key in stats, key
        assert stats["canvas"] == [300, 200]
        assert stats["bbox"] == [120, 80, 219, 159]

    def test_stats_are_json_native(self):
        """stats 要直接存进 JSON 列：混进 numpy 标量会在 commit 时炸掉，
        而那时候图其实已经算好了 —— 用户看到假失败。"""
        import json

        _, stats = cutout(_synthetic(), CutoutParams())
        json.dumps(stats)   # 不抛异常即通过

    def test_low_contrast_image_warns(self):
        """跨度太小（主体和背景太近）要出警告，而不是安静地给一张脏图。"""
        rgb = np.full((100, 100, 3), 200, dtype=np.uint8)
        rgb[40:60, 40:60] = 185      # 只差 15 档
        _, stats = cutout(rgb, CutoutParams())
        assert stats["warnings"], "跨度只有 15 档，应当给出提示"


# --------------------------------------------------------------------------
# 边界：三种会直接改变任务结果的情况
# --------------------------------------------------------------------------


class TestFailures:
    """失败必须是 CutoutError —— 继承 Exception，worker 才接得住。"""

    def test_blank_image_raises_cutout_error(self):
        """过曝 / 空白的图：找不到比背景暗的主体。"""
        blank = np.full((100, 100, 3), 250, dtype=np.uint8)
        with pytest.raises(CutoutError, match="没有比背景暗的主体"):
            cutout(blank, CutoutParams())

    def test_cutout_error_is_not_systemexit(self):
        """回归：算法绝不能用 SystemExit 报错。

        SystemExit 继承 BaseException，后台 worker 的 `except Exception` 抓不住，
        会静默杀死工作线程 —— 之后所有任务永远停在「排队中」，而健康检查还是绿的。
        """
        assert issubclass(CutoutError, Exception)
        assert not issubclass(CutoutError, SystemExit)

    def test_pixel_gate_rejects_oversized_image(self):
        with pytest.raises(CutoutError, match="超过单张上限"):
            check_pixel_count((10000, 10000), 30_000_000, what="原图")

    def test_pixel_gate_reports_the_count_in_wan_pixels(self):
        """回归：文案里的「万像素」必须真的是万像素。

        曾把像素数除以 1e6 却标成「万」，差 100 倍 —— 一张 10000×10000 的图会被
        说成「10000 万像素、超过上限 30 万像素」，而实际上限是 3000 万。
        """
        with pytest.raises(CutoutError) as excinfo:
            check_pixel_count((10000, 10000), 30_000_000, what="原图")

        message = str(excinfo.value)
        assert "10000 万像素" in message   # 1 亿像素 = 10000 万
        assert "3000 万像素" in message    # 3e7 像素 = 3000 万
        assert "30 万像素" not in message

    def test_pixel_gate_accepts_within_limit(self):
        check_pixel_count((1000, 1000), 30_000_000, what="原图")   # 不抛

    def test_sticker_bigger_than_page_raises(self, tmp_path):
        """贴纸比背景大：直接报错，别学脚本「打印一句警告然后照样裁掉」。

        批量场景下静默降质比报错难查得多。走的是 scale=None（原尺寸贴）这条路 ——
        给了缩放比例就缩得下，报错自然轮不到（那正是默认值改成 0.1 想达到的效果）。
        """
        source = tmp_path / "大图.png"
        Image.fromarray(_synthetic(), "RGB").save(source)
        page = Image.new("RGB", (100, 100), (0, 0, 255))

        with pytest.raises(CutoutError, match="比背景"):
            render_one(source, page, CutoutParams(scale=None), max_pixels=30_000_000)


class TestStickerSource:
    """已是透明底的输入不重抠。"""

    def test_already_transparent_input_is_used_as_is(self, tmp_path):
        """重抠会拿透明区的 RGB（均匀墨色）当输入，「纸」和「墨」一样黑，
        整幅画布都会被判成主体。手绘批次里混进已抠好的 PNG 是常见情况。"""
        rgba = np.zeros((60, 60, 4), dtype=np.uint8)
        rgba[20:40, 20:40] = (10, 10, 10, 255)

        source = tmp_path / "抠好的.png"
        Image.fromarray(rgba, "RGBA").save(source)

        sticker, stats = sticker_from(source, CutoutParams(), max_pixels=30_000_000)
        assert stats["already_transparent"] is True
        assert sticker.size == (60, 60)
        # 原样返回：四周仍然是透明的，没有被重新「抠」成不透明
        assert np.asarray(sticker)[...][0, 0, 3] == 0

    def test_opaque_png_goes_through_cutout(self, tmp_path):
        """没有透明通道的图老老实实走抠图流程。"""
        source = tmp_path / "原片.png"
        Image.fromarray(_synthetic(), "RGB").save(source)
        _, stats = sticker_from(source, CutoutParams(), max_pixels=30_000_000)
        assert stats["already_transparent"] is False
        assert stats["bbox_text"]

    def test_fully_opaque_rgba_png_is_still_cut(self, tmp_path):
        """「是 RGBA 文件」不等于「用上了透明通道」。

        判据是**存在不透明以下的像素**，不是「模式是 RGBA」。一张 alpha 处处 255
        的 PNG（导出时选了带透明通道、实际没抠）走的仍是完整抠图流程 —— 若把判据
        写成 `alpha.max() == 255` 或干脆只看 mode，这种图会被原样贴上去，
        纸底跟着一起盖到背景上。
        """
        rgba = np.full((40, 40, 4), 255, dtype=np.uint8)
        rgba[..., :3] = PAPER
        rgba[10:30, 10:30, :3] = INK      # 一块深色主体，alpha 仍是 255

        source = tmp_path / "全实心.png"
        Image.fromarray(rgba, "RGBA").save(source)

        _, stats = sticker_from(source, CutoutParams(), max_pixels=30_000_000)
        assert stats["already_transparent"] is False
        assert stats["bbox_text"]


class TestComposite:
    """贴合：默认缩到背景宽的 10% 居中；scale=None 时按原尺寸居中。"""

    def test_center_is_ink_corner_is_page(self):
        rgba, _ = cutout(_synthetic(), CutoutParams())
        sticker = Image.fromarray(rgba, "RGBA")
        page = Image.new("RGB", (400, 400), (0, 0, 255))

        out, _pos = composite(sticker, page, CutoutParams())
        assert out.size == (400, 400)
        assert out.getpixel((200, 200)) == (INK, INK, INK)     # (x, y)
        assert out.getpixel((5, 5)) == (0, 0, 255)             # 角落仍是背景色

    def test_scale_shrinks_and_trims_the_sticker(self):
        """给了 scale 才裁边 + 缩放（贴纸宽 = 底图宽 × scale）。"""
        rgba, _ = cutout(_synthetic(), CutoutParams())
        sticker = Image.fromarray(rgba, "RGBA")
        page = Image.new("RGB", (200, 200), (0, 0, 255))

        out, pos = composite(sticker, page, CutoutParams(scale=0.5))
        assert pos[0] >= 0 and pos[1] >= 0
        assert out.getpixel((100, 100)) == (INK, INK, INK)

    def test_default_scale_is_a_tenth_of_the_page(self):
        """不传任何参数时，贴纸宽度 = 底图宽的 10%，而不是脚本的「原尺寸」。

        这是**刻意偏离脚本默认**的一处：手绘原图动辄 1080×1440，随手挑的背景常常
        比它小，「原尺寸贴」会让整批图判失败。改回 None 等于把那个坑重新挖开。
        """
        assert CutoutParams().scale == 0.1

        rgba, _ = cutout(_synthetic(), CutoutParams())
        sticker = Image.fromarray(rgba, "RGBA")
        page = Image.new("RGB", (400, 400), (0, 0, 255))

        # 贴纸被缩到 400 × 0.1 = 40px 宽，落点自然按这个宽度居中
        _out, pos = composite(sticker, page, CutoutParams())
        assert pos[0] == (400 - 40) // 2

    def test_opacity_scales_the_sticker_alpha(self):
        rgba, _ = cutout(_synthetic(), CutoutParams())
        sticker = Image.fromarray(rgba, "RGBA")
        page = Image.new("RGB", (400, 400), (0, 0, 255))

        out, _ = composite(sticker, page, CutoutParams(opacity=0.5))
        # 半透明墨色压在纯蓝上：既不是纯墨，也不是纯背景
        assert out.getpixel((200, 200)) not in ((INK, INK, INK), (0, 0, 255))


class TestLoadPage:
    """背景图读取。"""

    def test_missing_page_raises(self, tmp_path):
        with pytest.raises(CutoutError, match="找不到背景图"):
            load_page(tmp_path / "没有.png", max_pixels=30_000_000)

    def test_page_is_read_as_rgb(self, tmp_path):
        path = tmp_path / "背景.png"
        Image.new("RGB", (50, 80), (0, 0, 255)).save(path)
        page = load_page(path, max_pixels=30_000_000)
        assert page.mode == "RGB"
        assert page.size == (50, 80)

    def test_broken_file_raises_cutout_error(self, tmp_path):
        """损坏的文件要变成 CutoutError（这一张失败），不是崩溃。"""
        path = tmp_path / "坏的.png"
        path.write_bytes(b"not a png at all")
        with pytest.raises(CutoutError, match="读不出这张背景图"):
            load_page(path, max_pixels=30_000_000)


class TestParams:
    """参数从任务表的 JSON 列来，必须做白名单 + 防御性兜底。"""

    def test_from_mapping_ignores_unknown_keys(self):
        params = CutoutParams.from_mapping({"hi_frac": 0.8, "谁加的字段": 1, "rm": "-rf"})
        assert params.hi_frac == 0.8
        assert not hasattr(params, "谁加的字段")

    def test_from_mapping_tolerates_bad_types(self):
        """类型不合法时退回默认值而不是抛异常 —— 老任务存过的形状不该让新代码崩。"""
        params = CutoutParams.from_mapping({"margin": "不是数字", "gate": "yes"})
        assert params.margin == 20
        assert params.gate is True

    def test_none_is_preserved_for_meaningful_fields(self):
        """scale / pos / dark_frac 的 None 是有意义的值（= 用默认行为）。"""
        params = CutoutParams.from_mapping({"scale": None, "pos": None, "dark_frac": None})
        assert params.scale is None
        assert params.pos is None
        assert params.dark_frac is None

    def test_empty_mapping_gives_defaults(self):
        assert CutoutParams.from_mapping(None) == CutoutParams()
        assert CutoutParams.from_mapping({}) == CutoutParams()

    def test_as_dict_round_trips(self):
        params = CutoutParams(scale=0.5, pos="tl", dark_frac=0.22)
        assert CutoutParams.from_mapping(params.as_dict()) == params


class TestRenderOne:
    """一张图的完整流程（换背景任务真正调的就是它）。"""

    def test_end_to_end(self, tmp_path):
        """scale=None：抠图结果按原尺寸、原画布整体居中贴上去。"""
        source = tmp_path / "原图.png"
        Image.fromarray(_synthetic(), "RGB").save(source)
        page = Image.new("RGB", (400, 400), (0, 0, 255))

        out, stats = render_one(
            source, page, CutoutParams(scale=None), max_pixels=30_000_000
        )
        assert out.size == (400, 400)
        assert stats["page"] == [400, 400]
        assert stats["output"] == [400, 400]
        assert stats["sticker"] == [300, 200]
        assert stats["position"] == [50, 100]

    def test_auto_position_finds_a_blank_spot(self, tmp_path):
        """pos=auto 时去背景的空白处落点（不走中心）。"""
        source = tmp_path / "原图.png"
        Image.fromarray(_synthetic(), "RGB").save(source)
        page = Image.new("RGB", (400, 400), (0, 0, 255))
        # 在页面上方压一片「字迹」把中心区域占掉
        for y in range(140, 260):
            page.putpixel((200, y), (255, 255, 255))

        _out, stats = render_one(
            source, page, CutoutParams(pos="auto"), max_pixels=30_000_000
        )
        assert stats["position"] != [50, 100]
