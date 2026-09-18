"""素材目录规划（app/core/materials.py）的单元测试。

这个模块是「素材放哪儿」的唯一权威来源：列目录接口、环境自检、启动建骨架
都从它取路径。测试守住三件事：

1. 骨架是**四段齐全**的 —— 少建一段，新克隆的仓库就会退化成随便堆的一层；
2. 分段名是**白名单**的 —— 拼错的段名要立刻报错，不能默默生成一个新目录；
3. 路径解析**只有一处** —— ~ 在这里展开，调用方拿到的就该是展开后的绝对路径。
"""

from pathlib import Path

import pytest

from app.core.config import settings
from app.core.materials import (
    CLIPS,
    OUTPUT,
    SOURCE,
    SUBDIRS,
    SUBTITLE,
    ensure_materials_layout,
    materials_root,
    subdir,
)

#: 规划里的四个分段，写成字面量而不是从 SUBDIRS 取 —— 断言才有意义，
#: 否则拿 SUBDIRS 跟自己比，删掉一段测试照样绿。
EXPECTED_SUBDIRS = ["source", "clips", "subtitle", "output"]


def test_plan_has_exactly_four_segments():
    """规划就是按流程分的四段，顺序也固定（文档与日志都按这个顺序写）。"""
    assert list(SUBDIRS) == EXPECTED_SUBDIRS
    assert {SOURCE, CLIPS, SUBTITLE, OUTPUT} == set(EXPECTED_SUBDIRS)


def test_ensure_materials_layout_creates_all_subdirs(tmp_path, monkeypatch):
    """骨架建成：根目录 + 四个分段，每一段都是真目录。"""
    root = tmp_path / "materials"
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(root))

    assert ensure_materials_layout() == root
    for name in EXPECTED_SUBDIRS:
        assert (root / name).is_dir(), f"缺了 {name}/ 分段"


def test_ensure_materials_layout_is_idempotent(tmp_path, monkeypatch):
    """重复调用不该报错，也不该动已有内容 —— 启动时会跑，列目录时还会再跑。"""
    root = tmp_path / "materials"
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(root))
    (root / SOURCE).mkdir(parents=True)
    (root / SOURCE / "待切.mp4").write_bytes(b"v")

    ensure_materials_layout()
    ensure_materials_layout()

    assert (root / SOURCE / "待切.mp4").read_bytes() == b"v"


def test_ensure_materials_layout_expands_tilde(tmp_path, monkeypatch):
    """配置里写成 ~ 开头时在真实主目录下建，不生成一个字面量叫 ~ 的目录。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", "~/materials")

    root = ensure_materials_layout()
    assert root == tmp_path / "materials"
    assert (root / CLIPS).is_dir()


def test_materials_root_expands_tilde_without_creating(tmp_path, monkeypatch):
    """materials_root() 只解析路径：~ 展开成主目录，但不创建任何东西。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", "~/还没建的素材目录")

    root = materials_root()
    assert root == tmp_path / "还没建的素材目录"
    assert not root.exists(), "只是解析路径，不该顺手创建"


def test_subdir_returns_path_under_root(tmp_path, monkeypatch):
    """subdir() 取的就是根目录下的对应分段。"""
    root = tmp_path / "materials"
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(root))

    assert subdir(SOURCE) == root / "source"
    assert subdir(CLIPS) == root / "clips"
    assert subdir(SUBTITLE) == root / "subtitle"
    assert subdir(OUTPUT) == root / "output"


def test_subdir_rejects_unknown_name():
    """规划之外的段名直接报错 —— 拼错时宁可炸掉，也不要悄悄建个新目录。"""
    with pytest.raises(ValueError) as excinfo:
        subdir("sources")

    # 报错信息要带上可选值，否则读日志的人还得回来翻代码
    assert "sources" in str(excinfo.value)
    assert "source" in str(excinfo.value)


def test_subdir_does_not_create(tmp_path, monkeypatch):
    """subdir() 只算路径不建目录：调用方可能只是想拼个展示用的字符串。"""
    root = tmp_path / "materials"
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(root))

    path = subdir(CLIPS)
    assert path == root / "clips"
    assert not path.exists()


def test_materials_root_accepts_absolute_path(tmp_path, monkeypatch):
    """绝对路径原样使用（外接硬盘、素材盘都走这条）。"""
    target = tmp_path / "别的盘" / "素材"
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(target))

    assert materials_root() == Path(str(target))
    assert ensure_materials_layout() == target
    assert (target / OUTPUT).is_dir()
