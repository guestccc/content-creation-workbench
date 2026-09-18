"""混剪素材库（mix_library）的测试。

要点：
- 素材目录由用户添加（**不限于 materials/ 里**），注册表落在素材根的 .mix-sources.json；
- 首次运行把 materials/clips/ 作为默认目录写进注册表，删掉之后不会自己长回来；
- clip id 是 sha1(绝对路径)[:16]，稳定、URL-safe、与路径穿越攻击不兼容；
- 时长的索引缓存与缩略图缓存都在素材根下，不往用户自己的目录里写东西；
- 深度与条数都有上限（用户可能把主目录整个加进来）。

ffprobe 探测通过 monkeypatch 打桩，测试不需要真视频、不跑子进程。
"""

import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.materials import CLIPS, subdir
from app.services import mix_library
from app.services.mix_library import (
    REGISTRY_FILENAME,
    add_source,
    clip_id_for,
    invalidate_cache,
    list_sources,
    remove_source,
    resolve_clip,
    resolve_clips,
    scan_library,
    thumb_path_for,
)


def _write_videos(directory: Path, *names: str) -> Path:
    """在目录下造几个假视频文件。"""
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-video")
    return directory


@pytest.fixture()
def materials(tmp_path, monkeypatch):
    """素材根指向临时目录，并造好 clips/<组>/<片段>.mp4 的结构。"""
    root = tmp_path / "materials"
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(root))
    _write_videos(root / CLIPS / "原片A_scenes", "a_clip_001.mp4", "a_clip_002.mp4")
    _write_videos(root / CLIPS / "原片B_scenes", "b_clip_001.mp4")
    return root


@pytest.fixture()
def external(tmp_path):
    """一个完全在素材目录之外的视频目录（模拟外接硬盘上的素材）。"""
    return _write_videos(tmp_path / "外接硬盘" / "原片C", "c_001.mp4", "c_002.mp4")


@pytest.fixture(autouse=True)
def _stub_probe(monkeypatch):
    """把 ffprobe 探测打桩成固定 3.5 秒，避免测试起真子进程。"""
    monkeypatch.setattr(mix_library, "probe_duration", lambda path: 3.5)


def _clip_by_name(data, name: str) -> dict:
    return next(c for c in data["clips"] if c["name"] == name)


# --------------------------------------------------------------------------
# 素材目录注册表
# --------------------------------------------------------------------------


class TestSources:
    def test_first_run_seeds_clips_dir(self, materials):
        """首次扫描自动把 materials/clips 作为默认素材目录，并落盘注册表。"""
        data = scan_library()
        assert [s["path"] for s in data["sources"]] == [str(subdir(CLIPS))]
        assert data["sources"][0]["exists"] is True
        assert data["sources"][0]["clip_count"] == 3
        assert (materials / REGISTRY_FILENAME).is_file()

    def test_seed_is_empty_without_clips_dir(self, tmp_path, monkeypatch):
        """素材根下没有 clips/ 时不硬塞一个不存在的默认目录。"""
        monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(tmp_path / "nothing"))
        data = scan_library()
        assert data["sources"] == [] and data["clips"] == []

    def test_add_source_outside_materials(self, materials, external):
        """核心需求：素材可以来自任意目录，不限于 materials/ 里。"""
        source, created = add_source(str(external))
        assert created is True

        data = scan_library()
        assert [s["path"] for s in data["sources"]] == [str(subdir(CLIPS)), str(external)]
        assert len(data["clips"]) == 5

        clip = _clip_by_name(data, "c_001.mp4")
        assert clip["source_id"] == source["id"]
        assert clip["abs_path"] == str(external / "c_001.mp4")
        assert clip["rel_path"] == "c_001.mp4"
        assert clip["group"] == ""
        assert clip["duration"] == 3.5

    def test_nested_directory_becomes_group(self, materials, tmp_path):
        """目录里的子目录成为分组（相对素材目录的路径）。"""
        nested = _write_videos(tmp_path / "混剪素材" / "组1", "n.mp4")
        add_source(str(nested.parent))
        clip = _clip_by_name(scan_library(), "n.mp4")
        assert clip["group"] == "组1" and clip["rel_path"] == "组1/n.mp4"

    def test_add_is_idempotent_and_normalizes_path(self, materials, external):
        """同一个目录（含 ~/.. 等写法）只会注册一次。"""
        first, created = add_source(str(external))
        second, created_again = add_source(str(external / "." / "原片C" / ".."))
        assert created is True and created_again is False
        assert first["id"] == second["id"]
        assert len(list_sources()) == 2  # 默认的 clips + 这一个

    @pytest.mark.parametrize(
        "bad, keyword",
        [
            ("", "不能为空"),
            ("relative/path", "绝对路径"),
            ("/不存在的目录/xyz", "不存在"),
        ],
    )
    def test_add_rejects_bad_path(self, materials, bad, keyword):
        with pytest.raises(ValueError, match=keyword):
            add_source(bad)

    def test_add_rejects_file(self, materials):
        """给一个文件而不是目录，必须拒绝。"""
        target = materials / CLIPS / "原片A_scenes" / "a_clip_001.mp4"
        with pytest.raises(ValueError, match="不是一个目录"):
            add_source(str(target))

    def test_max_sources_limit(self, materials, external, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "MIX_MAX_SOURCES", 2)
        add_source(str(external))
        other = _write_videos(tmp_path / "再来一个", "x.mp4")
        with pytest.raises(ValueError, match="最多添加 2 个"):
            add_source(str(other))

    def test_remove_source_keeps_files(self, materials, external):
        """移除素材目录只改列表，磁盘上的文件一个都不能动。"""
        source, _ = add_source(str(external))
        assert remove_source(source["id"]) is True

        data = scan_library()
        assert [s["path"] for s in data["sources"]] == [str(subdir(CLIPS))]
        assert len(data["clips"]) == 3
        assert (external / "c_001.mp4").is_file()

    def test_remove_unknown_id(self, materials):
        assert remove_source("0" * 16) is False

    def test_removed_default_stays_removed(self, materials):
        """默认目录被删掉后，重新扫描不能自作主张加回来。"""
        default = list_sources()[0]
        assert remove_source(default["id"]) is True
        assert scan_library()["sources"] == []
        assert scan_library()["clips"] == []

    def test_missing_source_dir_is_tolerated(self, materials, external):
        """目录被删/盘没挂上：条目保留、标记 exists=false，扫描不报错。"""
        source, _ = add_source(str(external))
        for file in external.iterdir():
            file.unlink()
        external.rmdir()

        data = scan_library()
        entry = next(s for s in data["sources"] if s["id"] == source["id"])
        assert entry["exists"] is False and entry["clip_count"] == 0
        assert len(data["clips"]) == 3  # 默认目录的素材照常


# --------------------------------------------------------------------------
# 扫描细节
# --------------------------------------------------------------------------


class TestScan:
    def test_clip_id_is_stable_and_url_safe(self, materials):
        data1 = scan_library()
        invalidate_cache()
        data2 = scan_library()
        assert {c["id"] for c in data1["clips"]} == {c["id"] for c in data2["clips"]}
        for clip in data1["clips"]:
            # id 里不能有路径分隔符或中文 —— 这是它能当 URL 参数用的前提
            assert "/" not in clip["id"] and clip["id"].isascii()
            assert clip["id"] == clip_id_for(clip["abs_path"])

    def test_hidden_and_non_video_files_are_skipped(self, materials):
        group = materials / CLIPS / "原片A_scenes"
        (group / ".thumbs").mkdir()
        (group / ".thumbs" / "a_clip_001.jpg").write_bytes(b"jpg")
        (group / ".hidden.mp4").write_bytes(b"fake")
        (group / "笔记.txt").write_text("不是视频", encoding="utf-8")
        # 缓存目录也在素材根下，不能被当成素材
        (materials / mix_library.THUMB_DIR_NAME).mkdir(exist_ok=True)
        (materials / mix_library.THUMB_DIR_NAME / "x.jpg").write_bytes(b"jpg")
        assert len(scan_library()["clips"]) == 3

    def test_scan_respects_depth_limit(self, materials, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "MIX_SOURCE_SCAN_DEPTH", 1)
        _write_videos(tmp_path / "深" / "L1" / "L2", "deep.mp4")
        add_source(str(tmp_path / "深"))  # 素材目录 = 深，deep.mp4 在第 2 层
        # 文件确实在磁盘上，只是扫描够不着
        assert (tmp_path / "深" / "L1" / "L2" / "deep.mp4").is_file()
        assert "deep.mp4" not in {c["name"] for c in scan_library()["clips"]}

    def test_scan_truncates_large_source(self, materials, external, monkeypatch):
        monkeypatch.setattr(settings, "MIX_MAX_CLIPS_PER_SOURCE", 1)
        add_source(str(external))
        data = scan_library()
        entry = next(s for s in data["sources"] if s["path"] == str(external))
        assert entry["clip_count"] == 1 and entry["truncated"] is True

    def test_cross_source_dedupe(self, materials, external):
        """同一个文件被两个素材目录同时收录时只算一次。"""
        add_source(str(materials / CLIPS))
        add_source(str(materials))
        data = scan_library()
        paths = [c["abs_path"] for c in data["clips"]]
        assert len(paths) == len(set(paths))

    def test_nested_source_owns_its_clips(self, materials):
        """嵌套去重：更具体的目录赢，不归先加进来的父目录。

        默认目录 clips 是父目录；把 clips/原片A_scenes 再单独加一遍时，
        它那两条素材必须算在这个子目录名下 —— 否则用户会看到新加的目录
        「一条素材都没有」，而素材其实都在。
        """
        parent_id = list_sources()[0]["id"]
        nested, _ = add_source(str(materials / CLIPS / "原片A_scenes"))
        data = scan_library()

        by_source = {}
        for clip in data["clips"]:
            by_source.setdefault(clip["source_id"], []).append(clip["name"])
        assert sorted(by_source[nested["id"]]) == ["a_clip_001.mp4", "a_clip_002.mp4"]
        # 父目录少掉这两条，只剩另一组的
        assert by_source[parent_id] == ["b_clip_001.mp4"]
        # 总数不变：去重是把素材换个归属，不是复制一份
        assert len(data["clips"]) == 3

    def test_removing_nested_source_gives_clips_back_to_parent(self, materials):
        """子目录移除后，那些素材回到父目录名下（文件一直在，只是换了归属）。"""
        parent_id = list_sources()[0]["id"]
        nested, _ = add_source(str(materials / CLIPS / "原片A_scenes"))
        remove_source(nested["id"])

        data = scan_library()
        counts = {s["id"]: s["clip_count"] for s in data["sources"]}
        assert counts[parent_id] == 3
        assert len(data["clips"]) == 3

    def test_scan_only_returns_videos_of_added_sources(self, materials, external):
        """没被添加的目录里的视频不会出现在素材库里。"""
        names = {c["name"] for c in scan_library()["clips"]}
        assert "c_001.mp4" not in names


# --------------------------------------------------------------------------
# 时长索引缓存
# --------------------------------------------------------------------------


class TestDurationCache:
    def test_cache_written_and_hit(self, materials, monkeypatch):
        scan_library()
        index_path = materials / mix_library.INDEX_FILENAME
        assert index_path.is_file()

        calls = []
        monkeypatch.setattr(
            mix_library, "probe_duration", lambda path: calls.append(path) or 3.5
        )
        data = scan_library()
        assert calls == []
        assert all(c["duration"] == 3.5 for c in data["clips"])

    def test_cache_invalidates_on_mtime_change(self, materials, monkeypatch):
        scan_library()
        target = materials / CLIPS / "原片A_scenes" / "a_clip_001.mp4"
        target.write_bytes(b"fake-video-changed")

        calls = []
        monkeypatch.setattr(
            mix_library, "probe_duration", lambda path: calls.append(path) or 9.9
        )
        data = scan_library()
        assert len(calls) == 1
        assert _clip_by_name(data, "a_clip_001.mp4")["duration"] == 9.9

    def test_corrupt_cache_is_rebuilt(self, materials):
        scan_library()
        (materials / mix_library.INDEX_FILENAME).write_text("not-json{{{", encoding="utf-8")
        assert len(scan_library()["clips"]) == 3

    def test_cache_prunes_removed_source(self, materials, external):
        """素材目录移除后，它的缓存条目也要跟着清掉。"""
        source, _ = add_source(str(external))
        scan_library()
        remove_source(source["id"])
        scan_library()
        entries = json.loads((materials / mix_library.INDEX_FILENAME).read_text(encoding="utf-8"))
        assert all(str(external) not in key for key in entries["entries"])

    def test_thumb_cache_is_central_and_pruned(self, materials):
        """缩略图缓存落在素材根下，不往素材目录里写；素材没了会被清理。"""
        clip = scan_library()["clips"][0]
        thumb = thumb_path_for(clip["id"])
        assert thumb.parent.name == mix_library.THUMB_DIR_NAME
        assert thumb.parent.parent == materials
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"jpg")

        # 素材文件消失后再次扫描：它对应的缩略图应当被回收
        Path(clip["abs_path"]).unlink()
        scan_library()
        assert not thumb.exists()


# --------------------------------------------------------------------------
# id 解析（安全边界）
# --------------------------------------------------------------------------


class TestResolve:
    def test_roundtrip(self, materials, external):
        add_source(str(external))
        for clip in scan_library()["clips"]:
            path = resolve_clip(clip["id"])
            assert path is not None and str(path) == clip["abs_path"]

    def test_rejects_unknown_id(self, materials):
        """伪造的 id（包括路径穿越意图）解析不到任何文件。"""
        assert resolve_clip("../etc/passwd") is None
        assert resolve_clip("0" * 16) is None
        assert resolve_clip("") is None

    def test_rejects_removed_source_clip(self, materials, external):
        """目录一旦移出素材列表，它的素材 id 立刻失效。"""
        source, _ = add_source(str(external))
        clip = _clip_by_name(scan_library(), "c_001.mp4")
        assert resolve_clip(clip["id"]) is not None

        remove_source(source["id"])
        assert resolve_clip(clip["id"]) is None

    def test_resolve_clips_batch(self, materials):
        ids = [c["id"] for c in scan_library()["clips"]]
        found = resolve_clips(ids + ["f" * 16])
        assert len(found) == 3
        assert resolve_clips([]) == {}
