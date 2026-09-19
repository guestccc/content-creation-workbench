"""配音产物磁盘侧（dubbing_library.py）的测试。

接口层（test_voicebox_api.py）只能验到「拦住了一个 URL」；这里直接打在函数上，
把命名规则、索引读写、路径解析这三组逻辑钉死 —— 尤其是
`resolve_audio` 的「只收裸文件名」，那是这层唯一的安全边界。

素材根一律指到 tmp（monkeypatch settings.SCENE_MATERIALS_DIR），
绝不往真实 materials/dubbing/ 里写东西。路径比较用 pathlib，不拼字符串
（Windows 反斜杠 / 非 ASCII 路径都会让字符串比较翻车）。
"""

import json

import pytest

from app.core.config import settings
from app.core.exceptions import BadRequestError
from app.services import dubbing_library


@pytest.fixture()
def dubbing_root(monkeypatch, tmp_path):
    """隔离素材根，返回 <tmp>/materials/dubbing（不预先创建）。"""
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(tmp_path / "materials"))
    return tmp_path / "materials" / "dubbing"


class TestNaming:
    def test_sanitize_keeps_chinese_and_spaces(self):
        assert dubbing_library.sanitize_base_name("第一个卖点(定稿) v2") == "第一个卖点(定稿) v2"

    def test_sanitize_strips_separators(self):
        """分隔符换掉而不是删掉：删了会把两个词粘成一坨。"""
        assert dubbing_library.sanitize_base_name("开场/结尾") == "开场_结尾"
        assert "/" not in dubbing_library.sanitize_base_name("a/b\\c")

    def test_sanitize_strips_windows_illegal_chars(self):
        cleaned = dubbing_library.sanitize_base_name('a<b>c:d"e|f?g*h')
        assert not any(char in cleaned for char in '<>:"|?*')

    def test_sanitize_trims_dots_and_spaces(self):
        """Windows 会静默吃掉结尾的点与空格，先去掉。"""
        assert dubbing_library.sanitize_base_name("  名字.  ") == "名字"

    def test_sanitize_is_length_capped(self):
        assert len(dubbing_library.sanitize_base_name("字" * 500)) == 100

    def test_allocate_does_not_overwrite(self, dubbing_root):
        first = dubbing_library.allocate_path("开场", ".wav")
        assert first.name == "开场.wav"
        first.write_bytes(b"x")

        second = dubbing_library.allocate_path("开场", ".wav")
        assert second.name == "开场-2.wav"
        second.write_bytes(b"x")
        assert dubbing_library.allocate_path("开场", ".wav").name == "开场-3.wav"

    def test_allocate_creates_directory(self, dubbing_root):
        assert not dubbing_root.exists()
        dubbing_library.allocate_path("开场", ".wav")
        assert dubbing_root.is_dir()

    def test_allocate_falls_back_to_timestamp_name(self, dubbing_root):
        path = dubbing_library.allocate_path("   ", ".wav")
        assert path.name.startswith("dub_")
        assert path.name.endswith(".wav")

    def test_allocate_rejects_unknown_extension(self, dubbing_root):
        """扩展名乱给就退回 .wav，不产生 .exe 这种产物名。"""
        assert dubbing_library.allocate_path("开场", ".exe").suffix == ".wav"


class TestResolveAudio:
    def test_resolves_existing_file(self, dubbing_root):
        target = dubbing_root / "开场.wav"
        dubbing_root.mkdir(parents=True)
        target.write_bytes(b"RIFF")

        assert dubbing_library.resolve_audio("开场.wav") == target

    def test_missing_file_is_none(self, dubbing_root):
        dubbing_root.mkdir(parents=True)
        assert dubbing_library.resolve_audio("没有这个.wav") is None

    @pytest.mark.parametrize(
        "name",
        ["../secret.wav", "..\\secret.wav", "sub/secret.wav", "/etc/passwd.wav", "..", ".", ""],
    )
    def test_traversal_and_junk_names_are_rejected(self, dubbing_root, name):
        """只收裸文件名 —— 这是本层唯一的安全边界，必须硬。"""
        with pytest.raises(BadRequestError):
            dubbing_library.resolve_audio(name)

    def test_non_audio_extension_is_rejected(self, dubbing_root):
        dubbing_root.mkdir(parents=True)
        (dubbing_root / "notes.txt").write_text("hi", encoding="utf-8")
        with pytest.raises(BadRequestError):
            dubbing_library.resolve_audio("notes.txt")


class TestIndexAndListing:
    def test_list_is_empty_when_dir_missing(self, dubbing_root):
        assert dubbing_library.list_audios() == []
        # 只读操作不该顺手建目录
        assert not dubbing_root.exists()

    def test_write_entry_round_trips(self, dubbing_root):
        dubbing_root.mkdir(parents=True)
        (dubbing_root / "开场.wav").write_bytes(b"RIFFdata")

        dubbing_library.write_entry(
            "开场.wav",
            duration=3.5,
            profile_id="p1",
            profile_name="我的声音",
            text_excerpt="大家好",
            size_bytes=8,
        )

        items = dubbing_library.list_audios()
        assert len(items) == 1
        assert items[0]["indexed"] is True
        assert items[0]["duration"] == 3.5
        assert items[0]["profile_name"] == "我的声音"
        assert items[0]["size_bytes"] == 8
        assert items[0]["name"] == "开场.wav"

    def test_unindexed_file_is_still_listed(self, dubbing_root):
        """用户自己拷进来的音频照样列出来，只是没有音色/时长。"""
        dubbing_root.mkdir(parents=True)
        (dubbing_root / "外部.mp3").write_bytes(b"ID3")

        items = dubbing_library.list_audios()
        assert [item["name"] for item in items] == ["外部.mp3"]
        assert items[0]["indexed"] is False
        assert items[0]["duration"] is None

    def test_non_audio_and_dotfiles_are_skipped(self, dubbing_root):
        dubbing_root.mkdir(parents=True)
        (dubbing_root / "开场.wav").write_bytes(b"RIFF")
        (dubbing_root / "说明.txt").write_text("x", encoding="utf-8")
        (dubbing_root / dubbing_library.INDEX_FILENAME).write_text("{}", encoding="utf-8")

        assert [item["name"] for item in dubbing_library.list_audios()] == ["开场.wav"]

    def test_sorted_newest_first(self, dubbing_root):
        dubbing_root.mkdir(parents=True)
        for name in ("旧.wav", "新.wav"):
            (dubbing_root / name).write_bytes(b"RIFF")
        dubbing_library.write_entry(
            "旧.wav", duration=1.0, profile_id="p", profile_name="", text_excerpt="", size_bytes=4
        )
        dubbing_library.write_entry(
            "新.wav", duration=2.0, profile_id="p", profile_name="", text_excerpt="", size_bytes=4
        )

        # 索引里的 created_at 都是「刚刚」，靠写入顺序无法区分；直接改索引时间戳
        index_path = dubbing_root / dubbing_library.INDEX_FILENAME
        payload = json.loads(index_path.read_text("utf-8"))
        payload["entries"]["旧.wav"]["created_at"] = 1_000_000.0
        payload["entries"]["新.wav"]["created_at"] = 2_000_000.0
        index_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        assert [item["name"] for item in dubbing_library.list_audios()] == ["新.wav", "旧.wav"]

    def test_list_limit_is_applied(self, dubbing_root, monkeypatch):
        monkeypatch.setattr(settings, "VOICEBOX_LIST_LIMIT", 2)
        dubbing_root.mkdir(parents=True)
        for i in range(5):
            (dubbing_root / f"a{i}.wav").write_bytes(b"RIFF")

        assert len(dubbing_library.list_audios()) == 2

    def test_corrupt_index_degrades_to_empty(self, dubbing_root):
        """索引只是附加信息：坏了就当作没有，清单照出。"""
        dubbing_root.mkdir(parents=True)
        (dubbing_root / "开场.wav").write_bytes(b"RIFF")
        (dubbing_root / dubbing_library.INDEX_FILENAME).write_text("{ 这不是 JSON", encoding="utf-8")

        items = dubbing_library.list_audios()
        assert [item["name"] for item in items] == ["开场.wav"]
        assert items[0]["indexed"] is False

    def test_index_with_wrong_shape_degrades_to_empty(self, dubbing_root):
        dubbing_root.mkdir(parents=True)
        (dubbing_root / "开场.wav").write_bytes(b"RIFF")
        (dubbing_root / dubbing_library.INDEX_FILENAME).write_text('["nope"]', encoding="utf-8")

        assert dubbing_library.list_audios()[0]["indexed"] is False

    def test_audio_url_is_quoted(self, dubbing_root):
        """文件名里的中文/空格要编码进 URL，否则前端放不出来。"""
        url = dubbing_library.audio_url_for("我的 开场.wav")
        assert "/voicebox/audios/" in url
        assert url.endswith("/file")
        assert " " not in url


class TestDelete:
    def test_delete_removes_file_and_entry(self, dubbing_root):
        dubbing_root.mkdir(parents=True)
        target = dubbing_root / "开场.wav"
        target.write_bytes(b"RIFF")
        dubbing_library.write_entry(
            "开场.wav", duration=1.0, profile_id="p", profile_name="", text_excerpt="", size_bytes=4
        )

        assert dubbing_library.delete_audio("开场.wav") is True
        assert not target.exists()
        assert dubbing_library.list_audios() == []

    def test_delete_missing_returns_false(self, dubbing_root):
        dubbing_root.mkdir(parents=True)
        assert dubbing_library.delete_audio("没有.wav") is False

    def test_delete_traversal_is_rejected(self, dubbing_root):
        dubbing_root.mkdir(parents=True)
        with pytest.raises(BadRequestError):
            dubbing_library.delete_audio("../secret.wav")


class TestMediaType:
    def test_known_extensions(self):
        assert dubbing_library.media_type_for("a.mp3") == "audio/mpeg"
        assert dubbing_library.media_type_for("a.WAV") == "audio/wav"

    def test_unknown_falls_back_to_wav(self):
        assert dubbing_library.media_type_for("a.bin") == "audio/wav"
