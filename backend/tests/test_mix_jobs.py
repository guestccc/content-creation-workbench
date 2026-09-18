"""混剪接口（/api/v1/mix/*）的测试。

覆盖：环境自检、素材目录增删、素材扫描、创建任务的各种校验拒绝、详情、取消、删除。
clip id 通过真实扫描临时素材库获得 —— 这样「不存在的 id 被拒绝」
与「路径穿越被拒绝」都是真验证，不是 mock 出来的假象。
"""

import pytest

from app.core.config import settings


@pytest.fixture()
def materials(tmp_path, monkeypatch):
    """素材根指向临时目录，造好 clips/<组>/<片段>.mp4；同时打桩 ffprobe。"""
    root = tmp_path / "materials"
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(root))
    group = root / "clips" / "原片A_scenes"
    group.mkdir(parents=True)
    for name in ("a.mp4", "b.mp4", "c.mp4", "d.mp4"):
        (group / name).write_bytes(b"fake-video")
    (root / "output").mkdir(parents=True)

    from app.services import mix_library
    monkeypatch.setattr(mix_library, "probe_duration", lambda path: 3.5)
    return root


def _make_videos(directory, *names) -> str:
    """素材根之外的普通视频目录（模拟用户在别处挑的素材）。"""
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"fake-video")
    return str(directory)


def _library_ids(client, *, source_id: str | None = None) -> list:
    resp = client.get("/api/v1/mix/library")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    clips = body["data"]["clips"]
    if source_id is not None:
        clips = [c for c in clips if c["source_id"] == source_id]
    return [c["id"] for c in clips]


def _id_of(client, name: str) -> str:
    """按文件名拿 clip id（素材库里重名时取第一个）。"""
    clips = client.get("/api/v1/mix/library").json()["data"]["clips"]
    return next(c["id"] for c in clips if c["name"] == name)


def _create_payload(ids, materials, count=1):
    return {
        "opening": [ids[0]],
        "middle": [ids[1], ids[2]],
        "ending": [ids[3]],
        "count": count,
        "output_dir": str(materials / "output"),
    }


class TestEnvironment:
    def test_environment(self, client, materials):
        resp = client.get("/api/v1/mix/environment")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "ready" in data
        assert data["default_output_dir"].endswith("output")
        # 添加素材目录的选择器默认落在镜头切片目录上
        assert data["default_source_dir"].endswith("clips")
        assert {d["name"] for d in data["dependencies"]} == {"ffmpeg", "ffprobe"}


class TestSources:
    def test_default_source_is_clips_dir(self, client, materials):
        """首次访问自动带上 materials/clips —— 老用户打开页面就有素材可选。"""
        resp = client.get("/api/v1/mix/sources")
        assert resp.status_code == 200
        sources = resp.json()["data"]
        assert len(sources) == 1
        assert sources[0]["path"].endswith("clips") and sources[0]["exists"] is True

    def test_add_source_outside_materials(self, client, materials, tmp_path):
        """核心能力：把素材根之外的任意目录加进来当素材。"""
        external = _make_videos(tmp_path / "外接硬盘" / "原片C", "c1.mp4", "c2.mp4")
        resp = client.post("/api/v1/mix/sources", json={"path": external})
        assert resp.status_code == 201
        source = resp.json()["data"]
        assert source["path"] == external and source["name"] == "原片C"

        data = client.get("/api/v1/mix/library").json()["data"]
        assert len(data["sources"]) == 2
        assert source["id"] in {c["source_id"] for c in data["clips"]}
        clip = next(c for c in data["clips"] if c["source_id"] == source["id"])
        assert clip["abs_path"] == f"{external}/c1.mp4"
        assert clip["thumb_url"].startswith("/api/v1/mix/library/clips/")

    def test_add_external_clip_is_playable(self, client, materials, tmp_path):
        """外部目录的素材同样能播放（Range 走的是服务端扫描结果）。"""
        external = _make_videos(tmp_path / "别处", "x.mp4")
        source = client.post("/api/v1/mix/sources", json={"path": external}).json()["data"]
        ids = _library_ids(client, source_id=source["id"])
        resp = client.get(
            f"/api/v1/mix/library/clips/{ids[0]}/video", headers={"range": "bytes=0-3"}
        )
        assert resp.status_code == 206 and resp.content == b"fake"

    def test_add_is_idempotent(self, client, materials, tmp_path):
        external = _make_videos(tmp_path / "别处", "x.mp4")
        first = client.post("/api/v1/mix/sources", json={"path": external}).json()["data"]
        second = client.post("/api/v1/mix/sources", json={"path": external}).json()["data"]
        assert first["id"] == second["id"]
        assert len(client.get("/api/v1/mix/sources").json()["data"]) == 2

    @pytest.mark.parametrize(
        "path, keyword",
        [
            ("/不存在的目录/xyz", "不存在"),
            ("relative/path", "绝对路径"),
            ("", None),  # 空串由 schema 的 min_length 拦下（422）
        ],
    )
    def test_add_rejects_bad_path(self, client, materials, path, keyword):
        resp = client.post("/api/v1/mix/sources", json={"path": path})
        if keyword is None:
            assert resp.status_code == 422
        else:
            assert resp.status_code == 400
            assert keyword in resp.json()["error"]["message"]

    def test_remove_source_keeps_files(self, client, materials, tmp_path):
        external = _make_videos(tmp_path / "别处", "x.mp4")
        source = client.post("/api/v1/mix/sources", json={"path": external}).json()["data"]

        resp = client.delete(f"/api/v1/mix/sources/{source['id']}")
        assert resp.status_code == 200
        assert len(client.get("/api/v1/mix/sources").json()["data"]) == 1
        # 素材文件一个都没动，只是不再从它取素材
        assert (tmp_path / "别处" / "x.mp4").is_file()
        assert client.get("/api/v1/mix/library").json()["data"]["clips"]
        assert all(
            c["source_id"] != source["id"]
            for c in client.get("/api/v1/mix/library").json()["data"]["clips"]
        )

    def test_remove_unknown_source_404(self, client, materials):
        assert client.delete(f"/api/v1/mix/sources/{'0' * 16}").status_code == 404


class TestLibrary:
    def test_library_returns_sources_and_clips(self, client, materials):
        resp = client.get("/api/v1/mix/library")
        data = resp.json()["data"]
        assert data["sources"][0]["clip_count"] == 4
        assert len(data["clips"]) == 4
        clip = data["clips"][0]
        assert clip["group"] == "原片A_scenes"
        assert clip["rel_path"] == f"原片A_scenes/{clip['name']}"
        assert clip["thumb_url"].startswith("/api/v1/mix/library/clips/")
        assert clip["video_url"].startswith("/api/v1/mix/library/clips/")

    def test_clip_video_range(self, client, materials):
        ids = _library_ids(client)
        resp = client.get(
            f"/api/v1/mix/library/clips/{ids[0]}/video",
            headers={"range": "bytes=0-3"},
        )
        assert resp.status_code == 206
        assert resp.content == b"fake"

    def test_clip_video_unknown_id_404(self, client, materials):
        resp = client.get(f"/api/v1/mix/library/clips/{'0' * 16}/video")
        assert resp.status_code == 404

    def test_clip_video_path_traversal_404(self, client, materials):
        """路径穿越意图的 id 解析不到任何文件。"""
        resp = client.get("/api/v1/mix/library/clips/..%2F..%2Fetc/video")
        assert resp.status_code == 404


class TestCreateJob:
    def test_create_success(self, client, materials):
        ids = _library_ids(client)
        resp = client.post("/api/v1/mix/jobs", json=_create_payload(ids, materials))
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["status"] == "pending"
        assert data["count"] == 1
        assert data["seed"] > 0
        assert len(data["outputs"]) == 1
        # order 里开头是用户选的第一条，结尾是最后一条
        order = data["outputs"][0]["order"]
        assert len(order) == 4
        assert order[0].endswith("a.mp4") and order[-1].endswith("d.mp4")

    def test_create_with_clips_from_other_directory(self, client, materials, tmp_path):
        """素材来自素材根之外的目录：任务里记的是绝对路径，能直接喂给 ffmpeg。"""
        external = _make_videos(tmp_path / "外接硬盘" / "原片C", "o1.mp4", "m1.mp4", "m2.mp4", "e1.mp4")
        client.post("/api/v1/mix/sources", json={"path": external})

        resp = client.post(
            "/api/v1/mix/jobs",
            json={
                "opening": [_id_of(client, "o1.mp4")],
                "middle": [_id_of(client, "m1.mp4"), _id_of(client, "m2.mp4")],
                "ending": [_id_of(client, "e1.mp4")],
                "count": 1,
                "output_dir": str(materials / "output"),
            },
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["opening"] == [f"{external}/o1.mp4"]
        order = data["outputs"][0]["order"]
        assert order[0] == f"{external}/o1.mp4"
        assert order[-1] == f"{external}/e1.mp4"
        assert all(path.startswith(external) for path in order)

    def test_create_multiple_outputs_have_distinct_middle(self, client, materials):
        ids = _library_ids(client)
        resp = client.post("/api/v1/mix/jobs", json=_create_payload(ids, materials, count=2))
        assert resp.status_code == 201
        outputs = resp.json()["data"]["outputs"]
        assert len(outputs) == 2
        assert outputs[0]["order"] != outputs[1]["order"]
        # 开头结尾一致，只有中间顺序不同
        assert outputs[0]["order"][0] == outputs[1]["order"][0]
        assert outputs[0]["order"][-1] == outputs[1]["order"][-1]

    def test_empty_list_rejected(self, client, materials):
        ids = _library_ids(client)
        payload = _create_payload(ids, materials)
        payload["ending"] = []
        resp = client.post("/api/v1/mix/jobs", json=payload)
        # schema 层的 min_length=1 先拦住（422），service 层的非空校验是兜底
        assert resp.status_code == 422

    def test_unknown_clip_id_rejected(self, client, materials):
        ids = _library_ids(client)
        payload = _create_payload(ids, materials)
        payload["middle"] = ["f" * 16, ids[2]]
        resp = client.post("/api/v1/mix/jobs", json=payload)
        assert resp.status_code == 400
        assert "素材不存在" in resp.json()["error"]["message"]

    def test_duplicate_in_list_rejected(self, client, materials):
        ids = _library_ids(client)
        payload = _create_payload(ids, materials)
        payload["middle"] = [ids[1], ids[1]]
        resp = client.post("/api/v1/mix/jobs", json=payload)
        assert resp.status_code == 400
        assert "重复" in resp.json()["error"]["message"]

    def test_count_beyond_permutations_rejected(self, client, materials):
        """中间只有 2 条时最多 2 种顺序，要 3 条必须被拒并说明上限。"""
        ids = _library_ids(client)
        resp = client.post(
            "/api/v1/mix/jobs", json=_create_payload(ids, materials, count=3)
        )
        assert resp.status_code == 400
        assert "最多只能排出 2 种" in resp.json()["error"]["message"]

    def test_output_dir_must_exist(self, client, materials):
        ids = _library_ids(client)
        payload = _create_payload(ids, materials)
        payload["output_dir"] = str(materials / "不存在的目录")
        resp = client.post("/api/v1/mix/jobs", json=payload)
        assert resp.status_code == 400
        assert "输出目录" in resp.json()["error"]["message"]


class TestJobLifecycle:
    def _create(self, client, materials, count=1) -> dict:
        ids = _library_ids(client)
        resp = client.post("/api/v1/mix/jobs", json=_create_payload(ids, materials, count))
        assert resp.status_code == 201
        return resp.json()["data"]

    def test_list_and_detail(self, client, materials):
        job = self._create(client, materials)
        resp = client.get("/api/v1/mix/jobs")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        # 列表不带成片明细
        assert data["items"][0]["outputs"] == []

        resp = client.get(f"/api/v1/mix/jobs/{job['id']}")
        assert resp.status_code == 200
        assert len(resp.json()["data"]["outputs"]) == 1

    def test_detail_404(self, client, materials):
        assert client.get("/api/v1/mix/jobs/9999").status_code == 404

    def test_cancel_pending(self, client, materials):
        job = self._create(client, materials)
        resp = client.post(f"/api/v1/mix/jobs/{job['id']}/cancel")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "cancelled"
        # 终态不能再取消
        assert client.post(f"/api/v1/mix/jobs/{job['id']}/cancel").status_code == 409

    def test_delete_terminal_job_keeps_files(self, client, materials, tmp_path):
        job = self._create(client, materials)
        client.post(f"/api/v1/mix/jobs/{job['id']}/cancel")
        resp = client.delete(f"/api/v1/mix/jobs/{job['id']}")
        assert resp.status_code == 200
        assert client.get(f"/api/v1/mix/jobs/{job['id']}").status_code == 404
        # 输出目录（此时还是空的）本身不被删 —— 删记录不动磁盘
        assert job["output_dir"]

    def test_delete_running_rejected(self, client, materials):
        job = self._create(client, materials)
        # pending 不是终态，删除必须被拒
        assert client.delete(f"/api/v1/mix/jobs/{job['id']}").status_code == 409

    def test_output_video_before_success_404(self, client, materials):
        """成片还没产出来时播放接口必须 404，不能泄露路径。"""
        job = self._create(client, materials)
        resp = client.get(f"/api/v1/mix/jobs/{job['id']}/outputs/1/video")
        assert resp.status_code == 404
