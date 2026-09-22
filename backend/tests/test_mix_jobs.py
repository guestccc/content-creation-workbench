"""混剪接口（/api/v1/mix/*）的测试。

覆盖：环境自检、素材目录增删、素材扫描、创建任务的各种校验拒绝、详情、取消、删除。
clip id 通过真实扫描临时素材库获得 —— 这样「不存在的 id 被拒绝」
与「路径穿越被拒绝」都是真验证，不是 mock 出来的假象。
"""

import pytest

from pathlib import Path

from app.core.config import settings
from app.models.mix_job import MixJob, MixOutputStatus


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

    def test_cancel_while_queued_marks_outputs_skipped(self, client, materials):
        """排队中就被取消：成片条目不能留在「等待中」。

        认领要求 status == pending，任务一旦置成 cancelled 就再也不会被认领，
        留在 pending 的条目既不会被执行、也不计入 failed / skipped。
        """
        job = self._create(client, materials, count=2)
        data = client.post(f"/api/v1/mix/jobs/{job['id']}/cancel").json()["data"]

        assert data["status"] == "cancelled"
        assert [out["status"] for out in data["outputs"]] == ["skipped", "skipped"]
        assert "已取消" in data["outputs"][0]["error_message"]
        assert data["skipped_outputs"] == 2
        assert data["failed_outputs"] == 0 and data["completed_outputs"] == 0

    def test_cancelled_while_queued_can_be_retried(self, client, materials):
        """回归：排队中被取消的任务必须能单条重试。

        条目若留在 pending，重试会被 409 拒掉（「只有失败或跳过的条目才能重试」），
        而页面上一个重试入口都不会出现。
        """
        job = self._create(client, materials, count=2)
        client.post(f"/api/v1/mix/jobs/{job['id']}/cancel")

        response = client.post(f"/api/v1/mix/jobs/{job['id']}/items/2/retry")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["status"] == "pending"
        # 只有被点名的那条回队，另一条保持「已跳过」
        assert [out["status"] for out in data["outputs"]] == ["skipped", "pending"]

    def test_cancelled_while_queued_can_be_retried_all(self, client, materials):
        """回归：整批取消后「一键全部重试」要有候选，不能一条都找不到。"""
        job = self._create(client, materials, count=2)
        client.post(f"/api/v1/mix/jobs/{job['id']}/cancel")

        response = client.post(f"/api/v1/mix/jobs/{job['id']}/retry")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["status"] == "pending"
        assert all(out["status"] == "pending" for out in data["outputs"])

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


class TestBatchDelete:
    """批量删除：整批成功或整批失败（POST /jobs/batch-delete）。"""

    def _create(self, client, materials) -> dict:
        ids = _library_ids(client)
        resp = client.post("/api/v1/mix/jobs", json=_create_payload(ids, materials))
        assert resp.status_code == 201
        return resp.json()["data"]

    def _create_terminal(self, client, materials) -> int:
        job = self._create(client, materials)
        client.post(f"/api/v1/mix/jobs/{job['id']}/cancel")
        return job["id"]

    def test_batch_delete_success(self, client, materials):
        ids = [self._create_terminal(client, materials) for _ in range(2)]

        response = client.post("/api/v1/mix/jobs/batch-delete", json={"ids": ids})
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"ids": ids, "count": 2}
        for job_id in ids:
            assert client.get(f"/api/v1/mix/jobs/{job_id}").status_code == 404

    def test_batch_delete_non_terminal_conflict_rolls_back(self, client, materials):
        terminal_id = self._create_terminal(client, materials)
        pending = self._create(client, materials)["id"]

        response = client.post(
            "/api/v1/mix/jobs/batch-delete", json={"ids": [terminal_id, pending]}
        )
        assert response.status_code == 409, response.text
        assert client.get(f"/api/v1/mix/jobs/{terminal_id}").status_code == 200

    def test_batch_delete_empty_ids_422(self, client):
        response = client.post("/api/v1/mix/jobs/batch-delete", json={"ids": []})
        assert response.status_code == 422, response.text


class TestDeletePurge:
    """删除任务时可选连产物一起清（purge_files）—— 混剪的产物是整个输出目录。"""

    def _terminal_with_products(self, client, materials) -> tuple[int, Path]:
        """造一条终态任务，并在输出目录里留下一条假成片。"""
        ids = _library_ids(client)
        resp = client.post("/api/v1/mix/jobs", json=_create_payload(ids, materials))
        assert resp.status_code == 201, resp.text
        job = resp.json()["data"]
        out_dir = Path(job["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "mix-001.mp4").write_bytes(b"fake-output")
        client.post(f"/api/v1/mix/jobs/{job['id']}/cancel")
        return job["id"], out_dir

    def test_delete_without_purge_keeps_products(self, client, materials):
        """默认只删记录：成片文件原样保留。"""
        job_id, out_dir = self._terminal_with_products(client, materials)

        response = client.delete(f"/api/v1/mix/jobs/{job_id}")
        assert response.status_code == 200, response.text
        assert (out_dir / "mix-001.mp4").is_file()

    def test_delete_with_purge_removes_products(self, client, materials):
        """purge_files=true：输出目录整棵删掉。"""
        job_id, out_dir = self._terminal_with_products(client, materials)

        response = client.delete(f"/api/v1/mix/jobs/{job_id}?purge_files=true")
        assert response.status_code == 200, response.text
        assert not out_dir.exists(), f"输出目录没清掉：{out_dir}"


class TestJobRemark:
    """任务备注：用户自己看的标记（PUT /jobs/{id}/remark）。"""

    def _create(self, client, materials) -> dict:
        ids = _library_ids(client)
        resp = client.post("/api/v1/mix/jobs", json=_create_payload(ids, materials))
        assert resp.status_code == 201, resp.text
        return resp.json()["data"]

    def test_update_remark_echoed_in_detail_and_list(self, client, materials):
        """备注写进去后，详情与列表都必须回显（列表用的是同一个响应模型）。"""
        job = self._create(client, materials)

        response = client.put(
            f"/api/v1/mix/jobs/{job['id']}/remark", json={"remark": "  待替换开头  "}
        )
        assert response.status_code == 200, response.text
        # 首尾空白由 schema 统一剥掉，避免「看着是空的其实不是」
        assert response.json()["data"]["remark"] == "待替换开头"

        detail = client.get(f"/api/v1/mix/jobs/{job['id']}")
        assert detail.json()["data"]["remark"] == "待替换开头"

        item = next(
            i for i in client.get("/api/v1/mix/jobs").json()["data"]["items"]
            if i["id"] == job["id"]
        )
        assert item["remark"] == "待替换开头"

    def test_empty_remark_clears_existing(self, client, materials):
        """空串是「清空」而不是「不更新」—— 与 PATCH 的缺省语义刻意不同。"""
        job = self._create(client, materials)
        client.put(f"/api/v1/mix/jobs/{job['id']}/remark", json={"remark": "写错了"})

        response = client.put(f"/api/v1/mix/jobs/{job['id']}/remark", json={"remark": ""})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["remark"] == ""
        assert client.get(f"/api/v1/mix/jobs/{job['id']}").json()["data"]["remark"] == ""

    def test_remark_too_long_422(self, client, materials):
        job = self._create(client, materials)
        response = client.put(
            f"/api/v1/mix/jobs/{job['id']}/remark", json={"remark": "备" * 201}
        )
        assert response.status_code == 422, response.text
        # 超长被拒时备注保持原值，不写半截进去
        assert client.get(f"/api/v1/mix/jobs/{job['id']}").json()["data"]["remark"] == ""

    def test_update_missing_job_404(self, client, materials):
        response = client.put("/api/v1/mix/jobs/9999/remark", json={"remark": "x"})
        assert response.status_code == 404, response.text
        assert response.json()["success"] is False


def _settle(db_session, job_id: int, statuses: dict, *, job_status: str = "partial"):
    """把任务摆成「跑完了、但有几条没成」的样子。

    接口测试不真起 ffmpeg（会取决于开发机装没装），所以直接写库把每条成片的
    状态摆好；任务级计数按同一份 statuses 数一遍，与真跑完之后的记录形状
    一致 —— 重试的断言（尤其是计数重算）才有意义。
    """
    job = db_session.get(MixJob, job_id)
    assert job is not None
    for item in job.outputs:
        item.status = statuses.get(item.index, MixOutputStatus.SUCCESS)
        if item.status == MixOutputStatus.SUCCESS:
            path = Path(job.output_dir) / f"{item.index:02d}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake-mp4")
            item.output_path = str(path)
            item.output_name = path.name
            item.size_bytes = path.stat().st_size
        else:
            item.error_message = f"第 {item.index} 条拼接失败了"

    job.status = job_status
    job.completed_outputs = len(job.outputs)
    job.failed_outputs = sum(
        1 for it in job.outputs if it.status == MixOutputStatus.FAILED
    )
    job.skipped_outputs = sum(
        1 for it in job.outputs if it.status == MixOutputStatus.SKIPPED
    )
    db_session.commit()
    return job


class TestRetry:
    """单条重试：只有「没产出」的成片能重来，任务回到排队中。"""

    def _create(self, client, materials, count=2) -> dict:
        ids = _library_ids(client)
        resp = client.post("/api/v1/mix/jobs", json=_create_payload(ids, materials, count))
        assert resp.status_code == 201, resp.text
        return resp.json()["data"]

    def test_failed_output_is_requeued(self, client, db_session, materials):
        job = self._create(client, materials)
        _settle(db_session, job["id"], {2: MixOutputStatus.FAILED})

        response = client.post(f"/api/v1/mix/jobs/{job['id']}/items/2/retry")
        assert response.status_code == 200, response.text
        data = response.json()["data"]

        assert data["status"] == "pending"
        assert data["finished_at"] is None
        assert data["error_message"] == ""
        assert data["current_phase"] == "" and data["progress_percent"] == 0
        item = next(it for it in data["outputs"] if it["index"] == 2)
        assert item["status"] == "pending"
        assert item["error_message"] == ""
        assert item["output_path"] == "" and item["output_name"] == ""
        assert item["size_bytes"] == 0

    def test_order_is_untouched(self, client, db_session, materials):
        """order 决定这条成片到底怎么拼，重试绝不能换一版顺序。"""
        job = self._create(client, materials)
        _settle(db_session, job["id"], {2: MixOutputStatus.FAILED})
        before = next(it for it in job["outputs"] if it["index"] == 2)["order"]

        data = client.post(
            f"/api/v1/mix/jobs/{job['id']}/items/2/retry"
        ).json()["data"]
        after = next(it for it in data["outputs"] if it["index"] == 2)["order"]
        assert after == before

    def test_skipped_output_is_retryable_too(self, client, db_session, materials):
        """取消留下的 skipped 也要能重来 —— 它同样「没有产出」。"""
        job = self._create(client, materials)
        _settle(
            db_session,
            job["id"],
            {2: MixOutputStatus.SKIPPED},
            job_status="cancelled",
        )

        response = client.post(f"/api/v1/mix/jobs/{job['id']}/items/2/retry")
        assert response.status_code == 200, response.text
        outputs = response.json()["data"]["outputs"]
        assert next(it for it in outputs if it["index"] == 2)["status"] == "pending"
        # 没被点的那条第 1 条保持成功
        assert next(it for it in outputs if it["index"] == 1)["status"] == "success"

    def test_successful_output_cannot_be_retried(self, client, db_session, materials):
        job = self._create(client, materials)
        _settle(db_session, job["id"], {2: MixOutputStatus.FAILED})

        response = client.post(f"/api/v1/mix/jobs/{job['id']}/items/1/retry")
        assert response.status_code == 409
        assert "只有失败或跳过" in response.json()["error"]["message"]

    def test_unfinished_job_cannot_be_retried(self, client, materials):
        """还在排队 / 正在跑的任务不能重试（工作线程正拿着它）。"""
        job = self._create(client, materials)
        response = client.post(f"/api/v1/mix/jobs/{job['id']}/items/1/retry")
        assert response.status_code == 409
        assert "尚未结束" in response.json()["error"]["message"]

    def test_unknown_index_is_404(self, client, db_session, materials):
        job = self._create(client, materials)
        _settle(db_session, job["id"], {2: MixOutputStatus.FAILED})

        response = client.post(f"/api/v1/mix/jobs/{job['id']}/items/9/retry")
        assert response.status_code == 404
        assert "任务条目不存在" in response.json()["error"]["message"]

    def test_unknown_job_is_404(self, client):
        assert client.post("/api/v1/mix/jobs/999/items/1/retry").status_code == 404

    def test_stale_product_is_removed(self, client, db_session, materials):
        """重试前删掉这条的旧成片：路径按序号定死，留着会让判据失真。"""
        job = self._create(client, materials)
        _settle(db_session, job["id"], {2: MixOutputStatus.FAILED})
        out_dir = Path(job["output_dir"])
        stale = out_dir / "02.mp4"
        stale.write_bytes(b"half-written")            # 手工造一份残片
        partial = out_dir / "02.partial.mp4"
        partial.write_bytes(b"half")

        client.post(f"/api/v1/mix/jobs/{job['id']}/items/2/retry")
        assert not stale.exists() and not partial.exists()
        # 其它序号的成片不受影响
        assert (out_dir / "01.mp4").is_file()

    def test_counts_are_recounted(self, client, db_session, materials):
        """completed_outputs 是执行器 += 1 出来的、收尾不会重算，重试时必须重数。

        两条里第 2 条失败：重试后 completed 应当是「已成功的 1 条」，
        而不是原来的 2（那样这条重跑完会变成 3/2）。
        """
        job = self._create(client, materials)
        _settle(db_session, job["id"], {2: MixOutputStatus.FAILED})
        assert job["total_outputs"] == 2

        data = client.post(
            f"/api/v1/mix/jobs/{job['id']}/items/2/retry"
        ).json()["data"]
        assert data["completed_outputs"] == 1
        assert data["failed_outputs"] == 0
        assert data["skipped_outputs"] == 0


class TestRetryAll:
    """一键重试：把失败 + 跳过的成片一起重新入队，成功的原样不动。"""

    def _create(self, client, materials, count=3) -> dict:
        ids = _library_ids(client)
        payload = _create_payload(ids, materials, count)
        # 要 3 条成片就得有 3 条中间段（2 条素材最多排 2 种顺序，count=3 会被拒）
        payload["middle"] = [ids[1], ids[2], ids[3]]
        payload["ending"] = [ids[0]]
        resp = client.post("/api/v1/mix/jobs", json=payload)
        assert resp.status_code == 201, resp.text
        return resp.json()["data"]

    def test_every_unfinished_output_is_requeued(self, client, db_session, materials):
        job = self._create(client, materials)
        _settle(
            db_session,
            job["id"],
            {2: MixOutputStatus.FAILED, 3: MixOutputStatus.SKIPPED},
            job_status="cancelled",
        )
        kept = Path(job["output_dir"]) / "01.mp4"

        data = client.post(f"/api/v1/mix/jobs/{job['id']}/retry").json()["data"]
        assert data["status"] == "pending"
        assert [it["status"] for it in data["outputs"]] == ["success", "pending", "pending"]
        assert data["completed_outputs"] == 1
        assert data["failed_outputs"] == 0 and data["skipped_outputs"] == 0
        assert kept.read_bytes() == b"fake-mp4"

    def test_second_call_is_409(self, client, db_session, materials):
        """第一次调用就把任务置回 pending，第二次必须 409 —— 这正是「批量不能
        在前端循环调单条」要防的场景（循环会在第二次 409 时半途而废）。"""
        job = self._create(client, materials, count=2)
        _settle(db_session, job["id"], {1: MixOutputStatus.FAILED})

        assert client.post(f"/api/v1/mix/jobs/{job['id']}/retry").status_code == 200
        second = client.post(f"/api/v1/mix/jobs/{job['id']}/retry")
        assert second.status_code == 409
        assert "尚未结束" in second.json()["error"]["message"]

    def test_nothing_to_retry_is_409(self, client, db_session, materials):
        job = self._create(client, materials, count=2)
        _settle(db_session, job["id"], {}, job_status="success")

        response = client.post(f"/api/v1/mix/jobs/{job['id']}/retry")
        assert response.status_code == 409
        assert "没有可重试的成片" in response.json()["error"]["message"]

    def test_unfinished_job_cannot_be_retried(self, client, materials):
        job = self._create(client, materials, count=2)
        assert client.post(f"/api/v1/mix/jobs/{job['id']}/retry").status_code == 409
