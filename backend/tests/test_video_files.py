"""视频清单枚举（core/video_files.py）测试。

这是从 scene_job_service 抽出来的公共模块，字幕提取也用它。这里把规则钉死，
两个功能的行为就不会悄悄分叉：
- 输入是文件：直接用，不查扩展名（用户显式点名了它）；
- 输入是目录：按扩展名白名单枚举，跳过隐藏文件与 macOS 的 ._ 文件；
- 传了 files 就只处理勾选项，且勾选的文件名必须真实存在；
- 结果按路径小写排序，空清单与超上限都报 400。
"""

import pytest

from app.core.exceptions import BadRequestError
from app.core.video_files import enumerate_videos, is_video_file

EXTENSIONS = [".mp4", ".mov"]


def _make_dir(tmp_path, names):
    """造一个含假文件的输入目录，返回目录路径。"""
    source = tmp_path / "素材"
    source.mkdir(exist_ok=True)
    for name in names:
        (source / name).write_bytes(b"fake-video")
    return source


class TestIsVideoFile:
    """扩展名白名单判断。"""

    def test_match_is_case_insensitive(self):
        assert is_video_file("口播.MP4", EXTENSIONS)
        assert is_video_file("口播.mov", EXTENSIONS)

    def test_non_video_rejected(self):
        assert not is_video_file("字幕.srt", EXTENSIONS)
        assert not is_video_file("无扩展名", EXTENSIONS)


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
