"""一键成品的排版与滤镜串构造（finalcut_render 纯函数层）测试。

这里全部是纯函数：不换行真视频、不起子进程，只断言
- 换行/字号估算的规则（CJK≈1.0em、ASCII≈0.55em、`\n` 保留、行高 ≤ 框高）；
- drawtext 滤镜串的形状（**不能出现 Windows 盘符与反斜杠** —— 字体/文案
  只许用 `font.ttf`/`copy.txt` 两个 cwd 相对名）；
- 三套样式模板的参数差异与 boxborderw 的能力开关；
- 烧字 argv 的音频编码分支（aac/mp3 copy、其它转码、无音轨 -an）。
"""

import re

import pytest

from app.services.finalcut_render import (
    AUTO_MAX_FONT_SIZE,
    AUTO_MIN_FONT_SIZE,
    OUTLINE,
    TEXT_STYLES,
    WHITE_BOX,
    YELLOW,
    box_to_pixels,
    build_drawtext_filter,
    build_render_argv,
    estimate_char_width,
    fit_font_size,
    get_style,
    style_catalog,
    wrap_text,
    write_drawtext_files,
)

#: 滤镜串里不允许出现 Windows 盘符路径（C:\... 或 C:/...）。
#: 注意不能断言「没有 :」——冒号是 drawtext 的参数分隔符，本来就到处都是。
_DRIVE_PATH = re.compile(r"[A-Za-z]:[\\/]")


# --------------------------------------------------------------------------
# 字宽估算与换行
# --------------------------------------------------------------------------


class TestEstimateCharWidth:
    def test_cjk_is_full_em(self):
        assert estimate_char_width("中", 20) == pytest.approx(20.0)

    def test_fullwidth_punctuation_is_full_em(self):
        assert estimate_char_width("，", 20) == pytest.approx(20.0)

    def test_ascii_is_narrower(self):
        assert estimate_char_width("a", 20) == pytest.approx(11.0)
        assert estimate_char_width("a", 20) < estimate_char_width("中", 20)


class TestWrapText:
    def test_short_text_single_line(self):
        assert wrap_text("短文案", 720, 40) == ["短文案"]

    def test_wraps_at_box_width(self):
        # 4 个全角字 × 20px = 80px，框宽 40px → 每行两字
        assert wrap_text("中中中中", 40, 20) == ["中中", "中中"]

    def test_mixed_cjk_and_ascii(self):
        # a(11) b(11) 中(20) = 42 刚好放下；再来 c 就超了
        assert wrap_text("ab中cd", 42, 20) == ["ab中", "cd"]

    def test_newlines_are_respected(self):
        lines = wrap_text("第一行\n第二行", 720, 40)
        assert lines == ["第一行", "第二行"]

    def test_empty_lines_dropped(self):
        assert wrap_text("第一行\n\n\n第二行\n", 720, 40) == ["第一行", "第二行"]

    def test_single_char_wider_than_box_still_forms_a_line(self):
        # 一个字就比框宽：也不能把它丢掉（丢了 ffmpeg 就没字可烧）
        assert wrap_text("中", 5, 20) == ["中"]

    def test_empty_text_gives_no_lines(self):
        assert wrap_text("", 720, 40) == []
        assert wrap_text("  \n  ", 720, 40) == []


# --------------------------------------------------------------------------
# 字号自适应
# --------------------------------------------------------------------------


class TestFitFontSize:
    def test_taller_box_allows_bigger_font(self):
        """字号单调性：同样的文案与框宽，框越高字号越大（或持平）。"""
        text = "这是一条用来测试字号自适应的广告文案，长度适中"
        small, _, _ = fit_font_size(text, 300, 80)
        big, _, _ = fit_font_size(text, 300, 600)
        assert big >= small

    def test_wider_box_allows_bigger_font(self):
        text = "这是一条用来测试字号自适应的广告文案，长度适中"
        narrow, _, _ = fit_font_size(text, 200, 300)
        wide, _, _ = fit_font_size(text, 800, 300)
        assert wide >= narrow

    def test_total_height_within_box(self):
        """非截断时：行数×字号 + 行距×(行数-1) ≤ 框高 × 1.15（松弛系数）。"""
        text = "双十一大促，全场五折起，先到先得，错过再等一年"
        box_h = 200
        size, lines, truncated = fit_font_size(text, 360, box_h)
        assert not truncated
        total_h = len(lines) * size + 8 * (len(lines) - 1)
        assert total_h <= box_h * 1.15

    def test_result_within_bounds(self):
        size, _, truncated = fit_font_size("短", 720, 400)
        assert not truncated
        assert AUTO_MIN_FONT_SIZE <= size <= AUTO_MAX_FONT_SIZE

    def test_oversized_text_truncates_at_min_size(self):
        """降到最小字号仍放不下 → 截断并打标记（由调用方写进 item error）。"""
        text = "字" * 500
        size, lines, truncated = fit_font_size(text, 100, 40)
        assert truncated
        assert size == AUTO_MIN_FONT_SIZE
        # 截断后的行数必须真的放得下了
        max_lines = len(wrap_text(text, 100, AUTO_MIN_FONT_SIZE))
        assert 1 <= len(lines) < max_lines

    def test_empty_text_does_not_crash(self):
        size, lines, truncated = fit_font_size("", 360, 200)
        assert size == AUTO_MIN_FONT_SIZE
        assert truncated is False
        assert lines == [""]

    def test_manual_newlines_survive_fitting(self):
        _, lines, _ = fit_font_size("上句\n下句", 720, 400)
        assert "上句" in lines and "下句" in lines


# --------------------------------------------------------------------------
# 归一化框 → 像素框
# --------------------------------------------------------------------------


class TestBoxToPixels:
    def test_full_frame(self):
        spec = {"width": 720, "height": 1280}
        assert box_to_pixels({"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}, spec) == (
            0, 0, 720, 1280,
        )

    def test_center_box(self):
        spec = {"width": 1000, "height": 500}
        assert box_to_pixels({"x": 0.25, "y": 0.1, "w": 0.5, "h": 0.2}, spec) == (
            250, 50, 500, 100,
        )

    def test_tiny_box_clamped_to_at_least_one_pixel(self):
        spec = {"width": 100, "height": 100}
        x, y, w, h = box_to_pixels({"x": 0.0, "y": 0.0, "w": 0.001, "h": 0.001}, spec)
        assert w >= 1 and h >= 1


# --------------------------------------------------------------------------
# drawtext 滤镜串
# --------------------------------------------------------------------------


class TestBuildDrawtextFilter:
    def test_white_box_full_params(self):
        f = build_drawtext_filter((100, 200, 400, 100), 40, WHITE_BOX, supports_boxborderw=True)
        assert f.startswith("drawtext=fontfile=font.ttf")
        assert "textfile=copy.txt" in f
        assert "fontsize=40" in f
        # 框中心 (300, 250)，居中交给 ffmpeg 实测的 text_w/text_h
        assert "x=300-text_w/2" in f
        assert "y=250-text_h/2" in f
        assert "fontcolor=white" in f
        assert "box=1" in f
        assert "boxcolor=black@0.45" in f
        assert "boxborderw=12" in f

    def test_expansion_disabled(self):
        """必须 expansion=none：文案里的裸 %（「立减100%」）会触发 drawtext 的
        %{...} 展开，只 warning 一句「Stray %」就把整条文案渲成空白（真机踩过）。"""
        for style in TEXT_STYLES.values():
            f = build_drawtext_filter((10, 20, 300, 80), 32, style, supports_boxborderw=True)
            assert "expansion=none" in f

    def test_no_windows_path_in_filter(self):
        """滤镜串里绝不能出现盘符路径与反斜杠（字体/文案是 cwd 相对名）。"""
        for style in TEXT_STYLES.values():
            f = build_drawtext_filter((10, 20, 300, 80), 32, style, supports_boxborderw=True)
            assert "\\" not in f
            assert not _DRIVE_PATH.search(f)
            assert "fontfile=font.ttf" in f
            assert "textfile=copy.txt" in f

    def test_boxborderw_follows_capability(self):
        """老 ffmpeg（drawtext 5.x 之前）不认 boxborderw，按探测结果省略。"""
        with_cap = build_drawtext_filter((0, 0, 100, 50), 20, WHITE_BOX, supports_boxborderw=True)
        without_cap = build_drawtext_filter((0, 0, 100, 50), 20, WHITE_BOX, supports_boxborderw=False)
        assert "boxborderw" in with_cap
        assert "boxborderw" not in without_cap
        # 不带 boxborderw 时底色参数本身仍然在
        assert "box=1" in without_cap

    def test_yellow_style_has_no_box(self):
        f = build_drawtext_filter((0, 0, 100, 50), 20, YELLOW, supports_boxborderw=True)
        assert "fontcolor=yellow" in f
        assert "box=1" not in f
        assert "boxcolor" not in f
        assert "borderw=3" in f

    def test_outline_style(self):
        f = build_drawtext_filter((0, 0, 100, 50), 20, OUTLINE, supports_boxborderw=True)
        assert "fontcolor=white" in f
        assert "bordercolor=black@0.9" in f
        assert "box=1" not in f


class TestStyleCatalog:
    def test_three_styles_and_one_default(self):
        catalog = style_catalog()
        assert {entry["key"] for entry in catalog} == {"white_box", "yellow", "outline"}
        assert sum(1 for entry in catalog if entry["default"]) == 1

    def test_unknown_style_raises(self):
        with pytest.raises(ValueError, match="未知的文案样式"):
            get_style("neon_pink")


# --------------------------------------------------------------------------
# 字体与文案落盘
# --------------------------------------------------------------------------


class TestWriteDrawtextFiles:
    def test_writes_relative_ascii_names(self, tmp_path):
        font_src = tmp_path / "微软雅黑.ttf"
        font_src.write_bytes(b"fake-font-bytes")
        tmp_dir = tmp_path / "tmp"

        font_name, copy_name = write_drawtext_files(tmp_dir, font_src, "第一行\n第二行")

        # 滤镜串里引用的就是这两个 ASCII 相对名
        assert (font_name, copy_name) == ("font.ttf", "copy.txt")
        assert (tmp_dir / "font.ttf").read_bytes() == b"fake-font-bytes"
        assert (tmp_dir / "copy.txt").read_text(encoding="utf-8") == "第一行\n第二行"

    def test_copy_txt_has_no_bom(self, tmp_path):
        """带 BOM 的 textfile 在部分 ffmpeg 构建上会渲成方框，首字节必须不是 BOM。"""
        font_src = tmp_path / "font.ttf"
        font_src.write_bytes(b"fake-font-bytes")
        write_drawtext_files(tmp_path / "tmp", font_src, "中文文案")
        data = (tmp_path / "tmp" / "copy.txt").read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf")

    def test_font_recopied_when_size_differs(self, tmp_path):
        font_src = tmp_path / "font.ttf"
        font_src.write_bytes(b"v1")
        tmp_dir = tmp_path / "tmp"
        write_drawtext_files(tmp_dir, font_src, "文案")
        font_src.write_bytes(b"v2-longer")
        write_drawtext_files(tmp_dir, font_src, "文案")
        assert (tmp_dir / "font.ttf").read_bytes() == b"v2-longer"


# --------------------------------------------------------------------------
# 烧字 argv（音频分支）
# --------------------------------------------------------------------------


def _argv(audio_codec: str):
    return build_render_argv(
        "/fake/ffmpeg", "成片.mp4", "01.partial.mp4",
        "drawtext=fontfile=font.ttf:textfile=copy.txt:fontsize=40:x=1:y=1",
        "progress.txt", audio_codec=audio_codec,
    )


def _value_after(argv, flag):
    return argv[argv.index(flag) + 1]


class TestBuildRenderArgv:
    def test_aac_copies_stream(self):
        argv = _argv("aac")
        assert _value_after(argv, "-c:a") == "copy"
        assert "-an" not in argv

    def test_mp3_copies_stream(self):
        assert _value_after(_argv("mp3"), "-c:a") == "copy"

    def test_other_codec_transcodes_to_aac(self):
        """mkv/mov 里的 vorbis/pcm/ac3 不能直接塞进 mp4，转 aac 128k。"""
        argv = _argv("pcm_s16le")
        assert _value_after(argv, "-c:a") == "aac"
        assert _value_after(argv, "-b:a") == "128k"

    def test_no_audio_track(self):
        argv = _argv("")
        assert "-an" in argv
        assert "-c:a" not in argv

    def test_common_shape(self):
        argv = _argv("aac")
        # 只映射视频 + 可选音轨，字幕/数据流丢掉
        assert _value_after(argv, "-map") == "0:v:0"
        assert "0:a:0?" in argv
        assert _value_after(argv, "-c:v") == "libx264"
        assert _value_after(argv, "-vf").startswith("drawtext=")
        assert _value_after(argv, "-movflags") == "+faststart"
        assert _value_after(argv, "-progress") == "progress.txt"
        # 输出文件必须是最后一个参数（FakeFfmpeg 与真实调用都按这个约定）
        assert argv[-1] == "01.partial.mp4"
