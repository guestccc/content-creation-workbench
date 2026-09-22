"""媒体清单枚举（core/media_files.py）测试。

这是从 scene_job_service 抽出来的公共模块，字幕提取（视频）和换背景（图片）
都用它。这里把规则钉死，几个功能的行为就不会悄悄分叉：
- 输入是文件：直接用，不查扩展名（用户显式点名了它）；
- 输入是目录：按扩展名白名单枚举，跳过隐藏文件与 macOS 的 ._ 文件；
- 传了 files 就只处理勾选项，且勾选的文件名必须真实存在；
- 结果按路径小写排序，空清单与超上限都报 400。

图片那一套额外钉住一件事：**错误文案里的名词跟着 kind 走**。对着一个全是
PNG 的目录说「没有可处理的视频文件」是纯粹的误导，而这两条路径共用同一份
实现，最容易在这里退化。
"""

import pytest

from app.core.exceptions import BadRequestError
from app.core.media_files import (
    enumerate_images,
    enumerate_media_files,
    enumerate_videos,
    is_media_file,
    media_type_for_image,
    media_type_for_video,
)

EXTENSIONS = [".mp4", ".mov"]
IMAGE_EXTENSIONS = [".png", ".jpg"]


def _make_dir(tmp_path, names):
    """造一个含假文件的输入目录，返回目录路径。"""
    source = tmp_path / "素材"
    source.mkdir(exist_ok=True)
    for name in names:
        (source / name).write_bytes(b"fake-video")
    return source


class TestIsMediaFile:
    """扩展名白名单判断。"""

    def test_match_is_case_insensitive(self):
        assert is_media_file("口播.MP4", EXTENSIONS)
        assert is_media_file("口播.mov", EXTENSIONS)

    def test_non_video_rejected(self):
        assert not is_media_file("字幕.srt", EXTENSIONS)
        assert not is_media_file("无扩展名", EXTENSIONS)

    def test_video_and_image_whitelists_are_independent(self):
        """两张白名单各管各的：视频白名单不认识 .png，图片白名单不认识 .mp4。

        这条不是废话 —— 换背景页复用了 /fs 的列目录接口，一旦有谁图省事把
        .png 塞进 SCENE_INPUT_EXTENSIONS，镜头分割页就会把图片当视频列出来。
        """
        assert not is_media_file("涂鸦.png", EXTENSIONS)
        assert not is_media_file("口播.mp4", IMAGE_EXTENSIONS)


class TestMediaTypes:
    """后缀 → Content-Type。给错类型，浏览器会下载而不是显示。"""

    def test_video_types(self):
        assert media_type_for_video("a.mp4") == "video/mp4"
        assert media_type_for_video("a.MOV") == "video/quicktime"

    def test_image_types(self):
        assert media_type_for_image("a.png") == "image/png"
        assert media_type_for_image("a.JPEG") == "image/jpeg"

    def test_unknown_falls_back_to_octet_stream(self):
        assert media_type_for_video("a.xyz") == "application/octet-stream"
        assert media_type_for_image("a.xyz") == "application/octet-stream"

    def test_svg_is_deliberately_absent(self):
        """SVG 不进图片白名单：它是唯一能携带脚本的图片格式，同源直出就是 XSS 面。"""
        assert media_type_for_image("带毒的.svg") == "application/octet-stream"


class TestEnumerateVideos:
    """输入路径展开成视频清单。"""

    def test_file_input_used_directly(self, tmp_path):
        """输入是单个文件：直接用，哪怕扩展名不在白名单。"""
        video = tmp_path / "奇怪格式.xyz"
        video.write_bytes(b"fake")
        assert enumerate_videos(
            str(video), recursive=False, extensions=EXTENSIONS, max_files=10
        ) == [video]

    def test_missing_path_rejected(self, tmp_path):
        with pytest.raises(BadRequestError, match="不存在"):
            enumerate_videos(
                str(tmp_path / "没有这里"), recursive=False,
                extensions=EXTENSIONS, max_files=10,
            )

    def test_directory_lists_only_videos(self, tmp_path):
        """目录模式：只收白名单扩展名，跳过隐藏文件与 ._ 开头的 AppleDouble。"""
        source = _make_dir(tmp_path, ["a.mp4", "b.txt", ".hidden.mp4", "._a.mp4"])
        videos = enumerate_videos(
            str(source), recursive=False, extensions=EXTENSIONS, max_files=10
        )
        assert [p.name for p in videos] == ["a.mp4"]

    def test_sorted_case_insensitively(self, tmp_path):
        source = _make_dir(tmp_path, ["B.mp4", "a.mp4", "C.mp4"])
        videos = enumerate_videos(
            str(source), recursive=False, extensions=EXTENSIONS, max_files=10
        )
        assert [p.name for p in videos] == ["a.mp4", "B.mp4", "C.mp4"]

    def test_recursive_descends_into_subdirs(self, tmp_path):
        source = _make_dir(tmp_path, ["a.mp4"])
        sub = source / "子目录"
        sub.mkdir()
        (sub / "b.mp4").write_bytes(b"fake")

        shallow = enumerate_videos(
            str(source), recursive=False, extensions=EXTENSIONS, max_files=10
        )
        deep = enumerate_videos(
            str(source), recursive=True, extensions=EXTENSIONS, max_files=10
        )
        assert [p.name for p in shallow] == ["a.mp4"]
        assert [p.name for p in deep] == ["a.mp4", "b.mp4"]

    def test_files_mode_keeps_only_checked(self, tmp_path):
        """勾选模式：只取勾选的那几个，顺序按路径排。"""
        source = _make_dir(tmp_path, ["a.mp4", "b.mp4", "c.mp4"])
        videos = enumerate_videos(
            str(source), recursive=False, files=["c.mp4", "a.mp4"],
            extensions=EXTENSIONS, max_files=10,
        )
        assert [p.name for p in videos] == ["a.mp4", "c.mp4"]

    def test_files_mode_missing_name_rejected(self, tmp_path):
        source = _make_dir(tmp_path, ["a.mp4"])
        with pytest.raises(BadRequestError, match="不存在"):
            enumerate_videos(
                str(source), recursive=False, files=["a.mp4", "不存在.mp4"],
                extensions=EXTENSIONS, max_files=10,
            )

    def test_empty_directory_rejected(self, tmp_path):
        source = _make_dir(tmp_path, ["只有文本.txt"])
        with pytest.raises(BadRequestError, match="没有可处理的视频"):
            enumerate_videos(
                str(source), recursive=False, extensions=EXTENSIONS, max_files=10
            )

    def test_max_files_enforced(self, tmp_path):
        source = _make_dir(tmp_path, ["a.mp4", "b.mp4", "c.mp4"])
        with pytest.raises(BadRequestError, match="最多处理 2 条"):
            enumerate_videos(
                str(source), recursive=False, extensions=EXTENSIONS, max_files=2
            )


class TestEnumerateImages:
    """图片走的是同一份实现，只有错误文案里的名词不同。"""

    def test_directory_lists_only_images(self, tmp_path):
        source = _make_dir(tmp_path, ["a.png", "b.jpg", "c.txt", "._a.png", "d.mp4"])
        images = enumerate_images(
            str(source), recursive=False, extensions=IMAGE_EXTENSIONS, max_files=10
        )
        assert [p.name for p in images] == ["a.png", "b.jpg"]

    def test_files_mode_keeps_only_checked(self, tmp_path):
        source = _make_dir(tmp_path, ["a.png", "b.png", "c.png"])
        images = enumerate_images(
            str(source), recursive=False, files=["c.png", "a.png"],
            extensions=IMAGE_EXTENSIONS, max_files=10,
        )
        assert [p.name for p in images] == ["a.png", "c.png"]

    def test_empty_directory_error_says_image_not_video(self, tmp_path):
        """空目录的文案必须是「图片」—— 对一个全是 PNG 的目录说「视频」纯属误导。"""
        source = _make_dir(tmp_path, ["只有文本.txt"])
        with pytest.raises(BadRequestError, match="没有可处理的图片文件"):
            enumerate_images(
                str(source), recursive=False, extensions=IMAGE_EXTENSIONS, max_files=10
            )

    def test_max_files_error_says_image(self, tmp_path):
        source = _make_dir(tmp_path, ["a.png", "b.png", "c.png"])
        with pytest.raises(BadRequestError, match="最多处理 2 条图片"):
            enumerate_images(
                str(source), recursive=False, extensions=IMAGE_EXTENSIONS, max_files=2
            )

    def test_kind_defaults_to_video(self, tmp_path):
        """kind 的默认值是「视频」，既有调用方一个字都不用改。"""
        source = _make_dir(tmp_path, ["只有文本.txt"])
        with pytest.raises(BadRequestError, match="没有可处理的视频文件"):
            enumerate_media_files(
                str(source), recursive=False, extensions=EXTENSIONS, max_files=10
            )
