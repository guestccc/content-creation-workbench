"""文件系统浏览接口测试。

守住两条底线：只读（路径必须是真实存在的目录）和
排序稳定（目录在前、同类按名称排序）。
"""


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


def test_tilde_expansion(client, tmp_path, monkeypatch):
    """~ 应展开为用户主目录。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "素材").mkdir()

    response = client.get("/api/v1/fs/list", params={"path": "~"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["path"] == str(tmp_path)
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


def test_default_path_is_home(client):
    """不传 path 时默认列用户主目录（不应报错）。"""
    response = client.get("/api/v1/fs/list")
    assert response.status_code == 200, response.text
