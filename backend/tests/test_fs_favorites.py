"""目录收藏夹（fs_favorites）的测试。

要点：
- 收藏清单是**全局一份**的 JSON（素材根的 .directory-favorites.json），不分页面/用途；
- 首次运行**不落盘**（打开弹窗看一眼不该在磁盘上留文件），与 mix 注册表的
  seed 行为刻意不同；
- 路径一律 resolve 后入库，「同一个目录只收藏一条」不依赖字符串怎么写的；
- 取消收藏**不动磁盘**；也**绝不往被收藏的用户目录里写任何东西**；
- 被收藏目录被删/拔盘后条目保留、exists 变 False（要能看见、也要能删掉）。

测试不碰真实 materials/（materials fixture 把素材根指到临时目录），不跑子进程。
"""

import json
import sys
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.exceptions import BadRequestError
from app.services.fs_favorites import (
    FAVORITES_FILENAME,
    add_favorite,
    favorite_id_for,
    list_favorites,
    remove_favorite,
)


@pytest.fixture()
def materials(tmp_path, monkeypatch):
    """素材根指向临时目录：收藏文件落在素材根，不能写进真实 materials/。"""
    root = tmp_path / "materials"
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(root))
    return root


@pytest.fixture()
def external(tmp_path):
    """一个完全在素材目录之外的目录（模拟外接硬盘上的素材）。"""
    directory = tmp_path / "外接硬盘" / "原片C"
    directory.mkdir(parents=True)
    (directory / "c_001.mp4").write_bytes(b"fake-video")
    return directory


class TestRegistry:
    def test_first_read_is_empty_and_writes_nothing(self, materials):
        """首次读取返回空列表，且不落盘（与 mix 注册表的 seed 行为刻意不同）。"""
        assert list_favorites() == []
        assert not (materials / FAVORITES_FILENAME).exists()

    def test_add_persists_and_returns_shape(self, materials, external):
        item, created = add_favorite(str(external))
        assert created is True
        assert item["id"] == favorite_id_for(external.resolve())
        assert item["path"] == str(external.resolve())
        assert item["name"] == "原片C"
        assert item["exists"] is True
        assert item["added_at"] > 0

        raw = json.loads((materials / FAVORITES_FILENAME).read_text(encoding="utf-8"))
        assert raw["version"] == 1
        assert [f["path"] for f in raw["favorites"]] == [str(external.resolve())]
        assert [item["id"] for item in list_favorites()] == [item["id"]]

    def test_add_is_idempotent_across_path_spellings(self, materials, external):
        """含 ..、带尾部斜杠的写法与原始写法解析到同一目录 → 同一条收藏。"""
        _, first = add_favorite(str(external))
        _, second = add_favorite(str(external / ".." / external.name))
        _, third = add_favorite(str(external) + "/")

        assert (first, second, third) == (True, False, False)
        favorites = list_favorites()
        assert len(favorites) == 1

    @pytest.mark.skipif(sys.platform != "win32", reason="大小写等价只在 Windows 文件系统上成立")
    def test_add_is_idempotent_across_case(self, materials, external):
        """Windows 上 resolve 返回磁盘真实大小写，swapcase 写法仍命中同一条。"""
        item, _ = add_favorite(str(external))
        _, created = add_favorite(str(external).swapcase())
        assert created is False
        assert [f["id"] for f in list_favorites()] == [item["id"]]

    @pytest.mark.parametrize(
        ("raw", "hint"),
        [
            ("", "不能为空"),
            ("relative/path", "绝对路径"),
        ],
    )
    def test_add_rejects_malformed_path(self, materials, raw, hint):
        with pytest.raises(BadRequestError, match=hint):
            add_favorite(raw)

    def test_add_rejects_missing_path(self, materials, tmp_path):
        with pytest.raises(BadRequestError, match="目录不存在"):
            add_favorite(str(tmp_path / "没有这个目录"))

    def test_add_rejects_file(self, materials, external):
        with pytest.raises(BadRequestError, match="不是一个目录"):
            add_favorite(str(external / "c_001.mp4"))

    def test_max_favorites_limit(self, materials, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "FS_MAX_FAVORITES", 2)
        for name in ("甲", "乙"):
            (tmp_path / name).mkdir()
            add_favorite(str(tmp_path / name))
        (tmp_path / "丙").mkdir()
        with pytest.raises(BadRequestError, match="最多 2 个"):
            add_favorite(str(tmp_path / "丙"))

    def test_name_falls_back_to_path_for_drive_root(self, materials, tmp_path):
        """盘符根 / 文件系统根没有目录名，name 回退为完整路径，侧栏不出现空白条目。"""
        item, _ = add_favorite(str(Path(tmp_path.anchor)))
        assert item["name"] == str(Path(tmp_path.anchor).resolve())

    def test_remove_keeps_directory_on_disk(self, materials, external):
        item, _ = add_favorite(str(external))
        assert remove_favorite(item["id"]) is True
        assert list_favorites() == []
        # 取消收藏不动磁盘：目录与其内容原样保留
        assert (external / "c_001.mp4").exists()

    def test_remove_unknown_id_returns_false(self, materials):
        assert remove_favorite("0" * 16) is False

    def test_missing_dir_keeps_entry_but_marks_stale(self, materials, external):
        item, _ = add_favorite(str(external))
        (external / "c_001.mp4").unlink()
        external.rmdir()
        favorites = list_favorites()
        assert len(favorites) == 1
        assert favorites[0]["id"] == item["id"]
        assert favorites[0]["exists"] is False
        # 失效条目仍可删除（移动硬盘换盘符后不能卡住删不掉）
        assert remove_favorite(item["id"]) is True
        assert list_favorites() == []

    def test_corrupt_file_degrades_to_empty(self, materials):
        materials.mkdir(parents=True)
        (materials / FAVORITES_FILENAME).write_text("not-json{{{", encoding="utf-8")
        assert list_favorites() == []
        # 损坏之后仍能正常写入（下次 _save 覆盖掉坏文件）
        good = materials / "好目录"
        good.mkdir()
        _, created = add_favorite(str(good))
        assert created is True
        assert len(list_favorites()) == 1

    def test_never_writes_into_favorited_dir(self, materials, external):
        add_favorite(str(external))
        assert {p.name for p in external.iterdir()} == {"c_001.mp4"}

    def test_id_is_url_safe_and_stable(self, external):
        first = favorite_id_for(external.resolve())
        second = favorite_id_for(external.resolve())
        assert first == second
        assert len(first) == 16
        assert first.isascii() and first.isalnum()
