"""文件系统浏览接口测试。

守住两条底线：只读（路径必须是真实存在的目录）和
排序稳定（目录在前、同类按名称排序）。

另外守住「素材目录」的便利不扩散：只有**不传 path** 时才落到素材目录
（并在它不存在时创建），显式传进来的路径一视同仁地严格校验。
"""

from pathlib import Path

from app.core.config import settings
from app.core.materials import SUBDIRS, ensure_materials_layout


def test_list_directory(client, tmp_path):
    """列目录：目录在前，视频文件单独标记，隐藏文件不展示。"""
    (tmp_path / "子目录").mkdir()
    (tmp_path / "b视频.mp4").write_bytes(b"b")
    (tmp_path / "a视频.MOV").write_bytes(b"a")
    (tmp_path / "说明.txt").write_text("t", encoding="utf-8")
    (tmp_path / ".隐藏").write_text("h", encoding="utf-8")

    response = client.get("/api/v1/fs/list", params={"path": str(tmp_path)})
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    names = [entry["name"] for entry in data["entries"]]
    assert names == ["子目录", "a视频.MOV", "b视频.mp4", "说明.txt"]
    assert data["entries"][0]["is_dir"] is True
    assert data["entries"][1]["is_video"] is True
    assert data["entries"][3]["is_video"] is False
    assert data["video_count"] == 2
    assert data["truncated"] is False
    assert data["path"] == str(tmp_path)


def test_list_root_parent_is_none(client):
    """已在根目录时 parent 为空。"""
    response = client.get("/api/v1/fs/list", params={"path": "/"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["parent"] is None


def test_tilde_expansion(client, fake_home):
    """~ 应展开为用户主目录。"""
    (fake_home / "素材").mkdir()

    response = client.get("/api/v1/fs/list", params={"path": "~"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["path"] == str(fake_home)
    assert [entry["name"] for entry in data["entries"]] == ["素材"]


def test_nonexistent_path_returns_400(client, tmp_path):
    """路径不存在 → 400。"""
    response = client.get("/api/v1/fs/list", params={"path": str(tmp_path / "不存在")})
    assert response.status_code == 400, response.text


def test_file_path_returns_400(client, tmp_path):
    """路径是文件而不是目录 → 400。"""
    target = tmp_path / "文件.mp4"
    target.write_bytes(b"v")
    response = client.get("/api/v1/fs/list", params={"path": str(target)})
    assert response.status_code == 400, response.text


def test_default_path_is_materials_dir(client, tmp_path, monkeypatch):
    """不传 path 时默认列素材目录（场景页打开时输入目录停在这里）。"""
    materials = tmp_path / "materials"
    materials.mkdir()
    (materials / "待切.mp4").write_bytes(b"v")
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(materials))

    response = client.get("/api/v1/fs/list")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["path"] == str(materials)
    # 列默认目录时会顺路把四个分段补齐，所以根下既有素材也有分段目录（目录在前）
    assert [entry["name"] for entry in data["entries"]] == [
        "clips",
        "output",
        "source",
        "subtitle",
        "待切.mp4",
    ]


def test_default_materials_dir_created_when_missing(client, tmp_path, monkeypatch):
    """素材目录还不存在时顺手建出来，而不是把第一次用的用户挡在 400 上。"""
    materials = tmp_path / "还没有的素材目录"
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(materials))
    assert not materials.exists()

    response = client.get("/api/v1/fs/list")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert materials.is_dir()
    # 建出来的是完整的四段骨架，不是一个光秃秃的空目录
    assert sorted(entry["name"] for entry in data["entries"]) == sorted(SUBDIRS)
    assert all(entry["is_dir"] for entry in data["entries"])


def test_explicit_missing_path_is_not_created(client, tmp_path):
    """显式传路径时不搞顺手创建：素材目录的便利只属于默认值。"""
    target = tmp_path / "不存在"
    response = client.get("/api/v1/fs/list", params={"path": str(target)})
    assert response.status_code == 400, response.text
    assert not target.exists()


def test_ensure_materials_dir_falls_back_to_home(tmp_path, monkeypatch):
    """素材目录建不出来（只读盘、权限不足）时退回主目录，而不是抛异常。

    素材目录只是个便利默认值，不能因为它把整个选目录流程卡死。
    """
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(tmp_path / "建不出来"))

    def _boom(self, *args, **kwargs):
        raise OSError("模拟只读盘")

    monkeypatch.setattr(Path, "mkdir", _boom)
    assert ensure_materials_layout() == Path.home()
