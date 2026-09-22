"""一键换背景接口测试：信封格式、输入校验、状态机与产物端点。

执行层（逐张抠图、取消语义、重启回收）的测试在 test_background_runner.py，
算法本身的测试在 test_cutout.py —— 这里只钉接口层的契约。

**不启动后台线程**（conftest 的 _no_background_worker 夹具）：接口测试只关心
「任务建出来了没有、状态对不对」，真去跑算法会让断言取决于开发机的图片。
需要「跑完」的任务在测试里手动把状态摆到终态。
"""

import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.background_job import (
    BackgroundJob,
    BackgroundJobItemStatus,
    BackgroundJobStatus,
)
from app.services import crawl_results
from app.services.background_job_service import BackgroundJobService
from tests.fakes import mc_note
# 执行层的夹具（假算法 + 连测试库的执行器）在 test_background_runner.py 里，
# 重试「真跑一遍」那条用例直接复用，免得在这里抄第二套假算法。
from tests.test_background_runner import _fake_render, _make_job, _make_runner, _refresh


@pytest.fixture(autouse=True)
def _isolate_materials(tmp_path, monkeypatch):
    """产物默认落在 materials/background/：不隔离会写进开发机的真实目录。"""
    monkeypatch.setattr(settings, "SCENE_MATERIALS_DIR", str(tmp_path / "materials"))


@pytest.fixture()
def image_dir(tmp_path) -> Path:
    """含三张图片 + 两个应被忽略的文件的输入目录。"""
    source = tmp_path / "原图"
    source.mkdir()
    for name in ("羊1.png", "羊2.PNG", "羊3.jpg"):
        (source / name).write_bytes(b"fake-image")
    (source / "说明.txt").write_text("不是图片", encoding="utf-8")
    (source / "._羊1.png").write_bytes(b"appledouble")
    return source


@pytest.fixture()
def background(tmp_path) -> Path:
    path = tmp_path / "背景.png"
    path.write_bytes(b"fake-bg")
    return path


def _create(client, source, background, **extra) -> dict:
    """创建任务并返回响应 data。"""
    payload = {
        "input_path": str(source),
        "background_path": str(background),
        **extra,
    }
    response = client.post("/api/v1/background/jobs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _error_message(response) -> str:
    return response.json()["error"]["message"]


class TestCreate:
    def test_envelope_and_fields(self, client, image_dir, background):
        data = _create(client, image_dir, background)

        assert data["status"] == "pending"
        assert data["total_images"] == 3
        assert data["completed_images"] == 0
        assert data["progress_percent"] == 0
        assert data["background_name"] == "背景.png"
        assert data["output_dir"]
        assert [item["source_name"] for item in data["items"]] == [
            "羊1.png", "羊2.PNG", "羊3.jpg",
        ]
        assert all(item["status"] == "pending" for item in data["items"])
        assert data["created_at"].endswith("Z")

    def test_enumeration_uses_the_image_whitelist(self, client, image_dir, background):
        """文本与 AppleDouble 不算图片 —— 后者是 macOS 上每张图旁边的伴生文件。"""
        data = _create(client, image_dir, background)
        names = [item["source_name"] for item in data["items"]]
        assert "说明.txt" not in names
        assert "._羊1.png" not in names

    def test_params_are_snapshotted_with_defaults(self, client, image_dir, background):
        data = _create(
            client, image_dir, background, params={"scale": 0.5, "pos": "tl"}
        )
        assert data["params"]["scale"] == 0.5
        assert data["params"]["pos"] == "tl"
        # 没传的字段也落进快照，执行时不必回头找默认值
        assert data["params"]["hi_frac"] == 0.90
        assert data["params"]["gate"] is True

    def test_default_scale_is_a_tenth(self, client, image_dir, background):
        """不传参数时缩放比例落到 0.1 = 占底图宽的 10%（脚本默认是「原尺寸」）。

        默认「原尺寸」的话，1080×1440 的手绘碰上随手挑的小背景会整批判失败 ——
        这条钉的就是那个默认值别再被改回去。
        """
        data = _create(client, image_dir, background)
        assert data["params"]["scale"] == 0.1

    def test_checked_subset(self, client, image_dir, background):
        data = _create(client, image_dir, background, files=["羊3.jpg", "羊1.png"])
        assert [item["source_name"] for item in data["items"]] == ["羊1.png", "羊3.jpg"]
        assert data["files"] == ["羊3.jpg", "羊1.png"]

    def test_custom_output_dir(self, client, image_dir, background, tmp_path):
        out = tmp_path / "我的产物"
        out.mkdir()
        data = _create(client, image_dir, background, output_dir=str(out))
        assert Path(data["output_dir"]).parent == out


class TestFromCrawlHandoff:
    """素材抓取带过来的图能不能直接建任务。

    这是两个功能之间的接缝，没有别的测试守着：抓取那边把一条笔记的图下载到
    `<输出目录>/<平台>/images/<笔记 id>/`（xhs 落盘的是 webp），对外只给
    「图片目录的绝对路径 + 相对路径清单」；换背景要的是「一个目录 + 一批纯文件名」。
    任一边改了口径（平台目录名、相对路径风格、图片白名单），前端那个「换背景」
    按钮就会整批失败 —— 所以这里拿**真的** collect_results 输出走一遍接口。
    """

    def test_crawled_note_images_can_be_handed_over(self, client, tmp_path, background):
        output_dir = tmp_path / "crawl"
        note_dir = output_dir / "xhs" / "images" / "n1"
        note_dir.mkdir(parents=True)
        for name in ("1.webp", "2.webp"):
            (note_dir / name).write_bytes(b"img")
        jsonl_dir = output_dir / "xhs" / "jsonl"
        jsonl_dir.mkdir(parents=True)
        (jsonl_dir / "search_contents_a.jsonl").write_text(
            json.dumps(mc_note("n1"), ensure_ascii=False) + "\n", encoding="utf-8"
        )

        notes = crawl_results.collect_results(output_dir, "xhs")
        assert len(notes) == 1
        note = notes[0]
        assert Path(note["local_image_dir"]) == note_dir

        # 前端在结果弹窗里做的翻译：相对路径取最后一段当文件名
        files = [rel.split("/")[-1] for rel in note["local_images"]]
        data = _create(client, note["local_image_dir"], background, files=files)

        assert data["total_images"] == 2
        assert [item["source_name"] for item in data["items"]] == ["1.webp", "2.webp"]


class TestCreateValidation:
    """创建时的校验必须在用户点「开始」的那一刻给出结果。"""

    def test_missing_background_is_400(self, client, image_dir, tmp_path):
        response = client.post(
            "/api/v1/background/jobs",
            json={
                "input_path": str(image_dir),
                "background_path": str(tmp_path / "没有.png"),
            },
        )
        assert response.status_code == 400
        assert "背景图不存在" in _error_message(response)

    def test_unsupported_background_extension_is_400(self, client, image_dir, tmp_path):
        """SVG 能携带脚本，不在白名单里 —— 也不该被当成背景图放进来。"""
        svg = tmp_path / "背景.svg"
        svg.write_text("<svg/>", encoding="utf-8")
        response = client.post(
            "/api/v1/background/jobs",
            json={"input_path": str(image_dir), "background_path": str(svg)},
        )
        assert response.status_code == 400
        assert "格式不受支持" in _error_message(response)

    def test_empty_directory_is_400_with_the_word_image(self, client, background, tmp_path):
        """文案必须说「图片」：对一个全是 PNG 的目录说「视频」是纯粹的误导。"""
        empty = tmp_path / "只有文本"
        empty.mkdir()
        (empty / "a.txt").write_text("x", encoding="utf-8")

        response = client.post(
            "/api/v1/background/jobs",
            json={"input_path": str(empty), "background_path": str(background)},
        )
        assert response.status_code == 400
        assert "图片" in _error_message(response)
        assert "视频" not in _error_message(response)

    def test_relative_path_is_422(self, client, image_dir, background):
        """相对路径直接拒 —— 后端 cwd 与用户敲命令时的 cwd 不是一回事。"""
        response = client.post(
            "/api/v1/background/jobs",
            json={"input_path": "原图", "background_path": str(background)},
        )
        assert response.status_code == 422

    def test_files_with_path_separator_is_422(self, client, image_dir, background):
        """勾选的文件名会与输入目录拼接，允许分隔符就是允许越出输入目录。"""
        response = client.post(
            "/api/v1/background/jobs",
            json={
                "input_path": str(image_dir),
                "background_path": str(background),
                "files": ["../../etc/passwd"],
            },
        )
        assert response.status_code == 422
        assert "路径分隔符" in response.text

    def test_checked_file_must_exist(self, client, image_dir, background):
        response = client.post(
            "/api/v1/background/jobs",
            json={
                "input_path": str(image_dir),
                "background_path": str(background),
                "files": ["没有这张.png"],
            },
        )
        assert response.status_code == 400
        assert "不存在" in _error_message(response)

    def test_out_of_range_params_are_422(self, client, image_dir, background):
        """参数范围在入口钉死：scale=0 这类值算法不会报错，只会安静地出一张废图。"""
        for params in (
            {"scale": 0},
            {"opacity": 2},
            {"hi_frac": 0.1, "lo_frac": 0.9},   # 透明线低于实心线 → 抠出反图
            {"pos": "centre"},                   # 拼错的关键字会静默退回默认行为
        ):
            response = client.post(
                "/api/v1/background/jobs",
                json={
                    "input_path": str(image_dir),
                    "background_path": str(background),
                    "params": params,
                },
            )
            assert response.status_code == 422, f"{params} 应当被拒"


class TestListAndDetail:
    def test_list_is_paginated_and_omits_items(self, client, image_dir, background):
        for _ in range(3):
            _create(client, image_dir, background)

        response = client.get("/api/v1/background/jobs", params={"page": 1, "page_size": 2})
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 3
        assert data["page"] == 1 and data["page_size"] == 2
        assert [item["id"] for item in data["items"]] == [3, 2]
        # 列表不背明细：一条任务可能有几百张，带上会让响应体大出一个量级
        assert all(item["items"] == [] for item in data["items"])

    def test_list_filters_by_status(self, client, image_dir, background):
        first = _create(client, image_dir, background)
        _create(client, image_dir, background)
        client.post(f"/api/v1/background/jobs/{first['id']}/cancel")

        data = client.get(
            "/api/v1/background/jobs", params={"status": "cancelled"}
        ).json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == first["id"]

    def test_detail_carries_items(self, client, image_dir, background):
        created = _create(client, image_dir, background)
        data = client.get(f"/api/v1/background/jobs/{created['id']}").json()["data"]
        assert len(data["items"]) == 3
        assert data["items"][0]["index"] == 1

    def test_unknown_job_is_404(self, client):
        response = client.get("/api/v1/background/jobs/999")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    def test_invalid_page_is_422(self, client):
        assert client.get("/api/v1/background/jobs", params={"page": 0}).status_code == 422


class TestStaticRouteOrder:
    def test_batch_delete_is_not_swallowed_by_the_id_route(self, client, image_dir, background):
        """/jobs/batch-delete 必须命中静态路由，不能被 /jobs/{job_id} 当 id 吞掉。"""
        job = _create(client, image_dir, background)
        client.post(f"/api/v1/background/jobs/{job['id']}/cancel")

        response = client.post(
            "/api/v1/background/jobs/batch-delete", json={"ids": [job["id"]]}
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"ids": [job["id"]], "count": 1}


class TestCancel:
    def test_cancel_pending(self, client, image_dir, background):
        job = _create(client, image_dir, background)
        response = client.post(f"/api/v1/background/jobs/{job['id']}/cancel")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "cancelled"
        assert data["finished_at"] is not None

    def test_cancel_twice_is_409(self, client, image_dir, background):
        job = _create(client, image_dir, background)
        client.post(f"/api/v1/background/jobs/{job['id']}/cancel")

        response = client.post(f"/api/v1/background/jobs/{job['id']}/cancel")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONFLICT"

    def test_cancel_unknown_is_404(self, client):
        assert client.post("/api/v1/background/jobs/999/cancel").status_code == 404

    def test_cancel_while_queued_marks_items_skipped(self, client, image_dir, background):
        """排队中就被取消：条目不能留在「等待中」。

        认领要求 status == pending，任务一旦置成 cancelled 就再也不会被认领，
        留在 pending 的条目既不会被执行、也不计入 failed / skipped ——
        详情页会出现「任务已取消，里面却躺着一堆等待中」。
        """
        job = _create(client, image_dir, background)
        data = client.post(f"/api/v1/background/jobs/{job['id']}/cancel").json()["data"]

        assert data["status"] == "cancelled"
        assert [it["status"] for it in data["items"]] == ["skipped"] * 3
        assert "已取消" in data["items"][0]["error_message"]
        # 这个任务不会再被认领，_finalize 也就永远不会跑：计数只能在取消时写
        assert data["skipped_images"] == 3
        assert data["failed_images"] == 0
        assert data["completed_images"] == 0

    def test_cancelled_while_queued_can_be_retried(self, client, image_dir, background):
        """回归：排队中被取消的任务必须能重试。

        「传完一批图，还没轮到就点了取消」是最常见的一条都没跑的情形。
        条目若留在 pending，单条与批量重试都会被 409 拒掉
        （「只有失败或跳过的条目才能重试」/「没有可重试的条目」），
        而页面上一个重试入口都不会出现。
        """
        job = _create(client, image_dir, background)
        client.post(f"/api/v1/background/jobs/{job['id']}/cancel")

        single = client.post(f"/api/v1/background/jobs/{job['id']}/items/1/retry")
        assert single.status_code == 200, single.text
        data = single.json()["data"]
        assert data["status"] == "pending"
        # 只有被点名的那一张回队，另外两张保持「已跳过」
        assert [it["status"] for it in data["items"]] == ["pending", "skipped", "skipped"]

    def test_cancelled_while_queued_can_be_retried_all(self, client, image_dir, background):
        """回归：整批取消后「一键全部重试」要有候选，不能一条都找不到。"""
        job = _create(client, image_dir, background)
        client.post(f"/api/v1/background/jobs/{job['id']}/cancel")

        response = client.post(f"/api/v1/background/jobs/{job['id']}/retry")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["status"] == "pending"
        assert [it["status"] for it in data["items"]] == ["pending"] * 3
        assert data["completed_images"] == 0


class TestRemark:
    def test_set_and_clear(self, client, image_dir, background):
        job = _create(client, image_dir, background)

        response = client.put(
            f"/api/v1/background/jobs/{job['id']}/remark", json={"remark": "  第一批  "}
        )
        assert response.status_code == 200
        assert response.json()["data"]["remark"] == "第一批"

        cleared = client.put(
            f"/api/v1/background/jobs/{job['id']}/remark", json={"remark": ""}
        )
        assert cleared.json()["data"]["remark"] == ""

    def test_too_long_is_422(self, client, image_dir, background):
        job = _create(client, image_dir, background)
        response = client.put(
            f"/api/v1/background/jobs/{job['id']}/remark", json={"remark": "字" * 201}
        )
        assert response.status_code == 422


class TestDelete:
    def test_pending_cannot_be_deleted(self, client, image_dir, background):
        job = _create(client, image_dir, background)
        response = client.delete(f"/api/v1/background/jobs/{job['id']}")
        assert response.status_code == 409
        assert "尚未结束" in _error_message(response)

    def test_delete_after_cancel(self, client, image_dir, background):
        job = _create(client, image_dir, background)
        client.post(f"/api/v1/background/jobs/{job['id']}/cancel")

        assert client.delete(f"/api/v1/background/jobs/{job['id']}").status_code == 200
        assert client.get(f"/api/v1/background/jobs/{job['id']}").status_code == 404

    def test_purge_files_removes_the_task_directory(self, client, image_dir, background):
        job = _create(client, image_dir, background)
        client.post(f"/api/v1/background/jobs/{job['id']}/cancel")
        output_dir = Path(job["output_dir"])
        (output_dir / "羊1.png").write_bytes(b"fake")

        client.delete(
            f"/api/v1/background/jobs/{job['id']}", params={"purge_files": True}
        )
        assert not output_dir.exists()

    def test_without_purge_files_are_kept(self, client, image_dir, background):
        job = _create(client, image_dir, background)
        client.post(f"/api/v1/background/jobs/{job['id']}/cancel")
        artifact = Path(job["output_dir"]) / "羊1.png"
        artifact.write_bytes(b"fake")

        client.delete(f"/api/v1/background/jobs/{job['id']}")
        assert artifact.exists()

    def test_batch_delete_rolls_back_as_a_whole(self, client, image_dir, background):
        """批里有一条不可删 → 整批回滚，且错误信息指出卡在哪条。"""
        done = _create(client, image_dir, background)
        pending = _create(client, image_dir, background)
        client.post(f"/api/v1/background/jobs/{done['id']}/cancel")

        response = client.post(
            "/api/v1/background/jobs/batch-delete",
            json={"ids": [done["id"], pending["id"]]},
        )
        assert response.status_code == 409
        assert f"#{pending['id']}" in _error_message(response)
        assert client.get(f"/api/v1/background/jobs/{done['id']}").status_code == 200

    def test_batch_delete_rejects_empty_list(self, client):
        assert (
            client.post("/api/v1/background/jobs/batch-delete", json={"ids": []}).status_code
            == 422
        )

    def test_batch_delete_rejects_absurd_batch_size(self, client):
        """单次批量上限 100：再多也不该在一次事务里做。"""
        response = client.post(
            "/api/v1/background/jobs/batch-delete",
            json={"ids": list(range(1, 200))},
        )
        assert response.status_code == 422


class TestItemOutput:
    """产物端点：只认真实产出的文件，路径完全由任务记录推导。"""

    def _job_with_artifact(self, client, image_dir, background) -> tuple[dict, Path]:
        job = _create(client, image_dir, background)
        item = job["items"][0]
        path = Path(item["output_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return job, path

    def test_serves_the_png(self, client, image_dir, background):
        job, path = self._job_with_artifact(client, image_dir, background)

        response = client.get(f"/api/v1/background/jobs/{job['id']}/items/1/output")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content == path.read_bytes()

    def test_unproduced_item_is_404(self, client, image_dir, background):
        job = _create(client, image_dir, background)
        response = client.get(f"/api/v1/background/jobs/{job['id']}/items/1/output")
        assert response.status_code == 404
        assert "还没有生成" in _error_message(response)

    def test_out_of_range_index_is_404(self, client, image_dir, background):
        """序号越界（任务里只有 3 张）回 404 —— 前端只关心「这张能不能显示」。"""
        job = _create(client, image_dir, background)
        for index in (4, 999):
            assert (
                client.get(
                    f"/api/v1/background/jobs/{job['id']}/items/{index}/output"
                ).status_code
                == 404
            )

    def test_non_positive_index_is_422(self, client, image_dir, background):
        """序号从 1 开始：0 在路由层就被挡掉（服务层另有一道兜底）。"""
        job = _create(client, image_dir, background)
        assert (
            client.get(f"/api/v1/background/jobs/{job['id']}/items/0/output").status_code
            == 422
        )

    def test_unknown_job_is_404(self, client):
        assert client.get("/api/v1/background/jobs/999/items/1/output").status_code == 404

    def test_non_numeric_index_is_422(self, client, image_dir, background):
        """序号必须是整数：路径参数不做字符串透传。"""
        job = _create(client, image_dir, background)
        response = client.get(f"/api/v1/background/jobs/{job['id']}/items/abc/output")
        assert response.status_code == 422


def _settle(db_session, job_id: int, statuses: dict, *, job_status: str = "partial"):
    """把任务摆成「跑完了、但有几张没成」的样子。

    接口测试不真跑算法（会取决于开发机的图片），所以直接写库把每张的状态摆好；
    任务级计数按同一份 statuses 数一遍，与真跑完之后的记录形状一致 ——
    重试的断言（尤其是计数重算）才有意义。
    """
    job = db_session.get(BackgroundJob, job_id)
    assert job is not None
    for item in job.items:
        item.status = statuses.get(item.index, BackgroundJobItemStatus.SUCCESS)
        if item.status == BackgroundJobItemStatus.SUCCESS:
            path = Path(item.output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        else:
            item.error_message = f"第 {item.index} 张失败了"

    job.status = job_status
    job.completed_images = len(job.items)
    job.failed_images = sum(
        1 for it in job.items if it.status == BackgroundJobItemStatus.FAILED
    )
    job.skipped_images = sum(
        1 for it in job.items if it.status == BackgroundJobItemStatus.SKIPPED
    )
    db_session.commit()
    return job


class TestRetry:
    """单条重试：只有「没产出」的条目能重来，任务回到排队中。"""

    def test_failed_item_is_requeued(self, client, db_session, image_dir, background):
        job = _create(client, image_dir, background)
        _settle(db_session, job["id"], {1: BackgroundJobItemStatus.FAILED})

        response = client.post(f"/api/v1/background/jobs/{job['id']}/items/1/retry")
        assert response.status_code == 200, response.text
        data = response.json()["data"]

        assert data["status"] == "pending"
        assert data["finished_at"] is None
        assert data["error_message"] == ""
        item = next(it for it in data["items"] if it["index"] == 1)
        assert item["status"] == "pending"
        assert item["error_message"] == ""
        assert item["stats"] == {}

    def test_skipped_item_is_retryable_too(self, client, db_session, image_dir, background):
        """取消 / 服务重启留下的 skipped 也要能重来 —— 它同样「没有产出」。"""
        job = _create(client, image_dir, background)
        _settle(
            db_session,
            job["id"],
            {2: BackgroundJobItemStatus.SKIPPED, 3: BackgroundJobItemStatus.SKIPPED},
            job_status="cancelled",
        )

        response = client.post(f"/api/v1/background/jobs/{job['id']}/items/2/retry")
        assert response.status_code == 200, response.text
        item = next(it for it in response.json()["data"]["items"] if it["index"] == 2)
        assert item["status"] == "pending"
        # 没被点的那张保持 skipped
        other = next(it for it in response.json()["data"]["items"] if it["index"] == 3)
        assert other["status"] == "skipped"

    def test_successful_item_cannot_be_retried(self, client, db_session, image_dir, background):
        job = _create(client, image_dir, background)
        _settle(db_session, job["id"], {1: BackgroundJobItemStatus.FAILED})

        response = client.post(f"/api/v1/background/jobs/{job['id']}/items/2/retry")
        assert response.status_code == 409
        assert "只有失败或跳过" in _error_message(response)

    def test_unfinished_job_cannot_be_retried(self, client, image_dir, background):
        """还在排队 / 正在跑的任务不能重试（工作线程正拿着它）。"""
        job = _create(client, image_dir, background)
        response = client.post(f"/api/v1/background/jobs/{job['id']}/items/1/retry")
        assert response.status_code == 409
        assert "尚未结束" in _error_message(response)

    def test_unknown_index_is_404(self, client, db_session, image_dir, background):
        job = _create(client, image_dir, background)
        _settle(db_session, job["id"], {1: BackgroundJobItemStatus.FAILED})

        response = client.post(f"/api/v1/background/jobs/{job['id']}/items/9/retry")
        assert response.status_code == 404
        assert "任务条目不存在" in _error_message(response)

    def test_unknown_job_is_404(self, client):
        assert client.post("/api/v1/background/jobs/999/items/1/retry").status_code == 404

    def test_stale_product_is_removed(self, client, db_session, image_dir, background):
        """重试前清掉这一张的旧产物：产物路径按序号定死，留着会让判据失真。"""
        job = _create(client, image_dir, background)
        _settle(db_session, job["id"], {1: BackgroundJobItemStatus.FAILED})
        item = db_session.get(BackgroundJob, job["id"]).items[0]
        stale = Path(item.output_path)
        stale.write_bytes(b"half written")     # 手工造一份残片

        client.post(f"/api/v1/background/jobs/{job['id']}/items/1/retry")
        assert not stale.exists()

    def test_completed_count_is_recounted(self, client, db_session, image_dir, background):
        """completed 是执行器 += 1 出来的、收尾不会重算，重试时必须按条目重数。

        3 张里第 3 张失败：重试后 completed 应当是「已成功的 2 张」，
        而不是原来的 3（那样这张重跑完会变成 4/3）。
        """
        job = _create(client, image_dir, background)
        _settle(db_session, job["id"], {3: BackgroundJobItemStatus.FAILED})
        assert job["total_images"] == 3

        data = client.post(
            f"/api/v1/background/jobs/{job['id']}/items/3/retry"
        ).json()["data"]
        assert data["completed_images"] == 2
        assert data["failed_images"] == 0
        assert data["skipped_images"] == 0


class TestRetryAll:
    """一键重试：把失败 + 跳过的条目一起重新入队，成功的原样不动。"""

    def test_every_unfinished_item_is_requeued(self, client, db_session, image_dir, background):
        job = _create(client, image_dir, background)
        _settle(
            db_session,
            job["id"],
            {
                2: BackgroundJobItemStatus.FAILED,
                3: BackgroundJobItemStatus.SKIPPED,
            },
            job_status="cancelled",
        )
        # 第 1 张成功的产物：重试不该碰它
        kept = Path(db_session.get(BackgroundJob, job["id"]).items[0].output_path)

        data = client.post(f"/api/v1/background/jobs/{job['id']}/retry").json()["data"]
        assert data["status"] == "pending"
        assert [it["status"] for it in data["items"]] == ["success", "pending", "pending"]
        assert data["completed_images"] == 1
        assert data["failed_images"] == 0 and data["skipped_images"] == 0
        assert kept.read_bytes() == b"\x89PNG\r\n\x1a\nfake"

    def test_second_call_is_409(self, client, db_session, image_dir, background):
        """第一次调用就把任务置回 pending，第二次必须 409 —— 这正是「批量不能
        在前端循环调单条」要防的场景（循环会在第二次 409 时半途而废）。"""
        job = _create(client, image_dir, background)
        _settle(db_session, job["id"], {1: BackgroundJobItemStatus.FAILED})

        assert client.post(f"/api/v1/background/jobs/{job['id']}/retry").status_code == 200
        second = client.post(f"/api/v1/background/jobs/{job['id']}/retry")
        assert second.status_code == 409
        assert "尚未结束" in _error_message(second)

    def test_nothing_to_retry_is_409(self, client, db_session, image_dir, background):
        job = _create(client, image_dir, background)
        _settle(db_session, job["id"], {}, job_status="success")

        response = client.post(f"/api/v1/background/jobs/{job['id']}/retry")
        assert response.status_code == 409
        assert "没有可重试的条目" in _error_message(response)

    def test_unfinished_job_cannot_be_retried(self, client, image_dir, background):
        job = _create(client, image_dir, background)
        assert (
            client.post(f"/api/v1/background/jobs/{job['id']}/retry").status_code == 409
        )


class TestRetryWithRunner:
    """重试之后真跑一遍：只处理被重试的那些，已成功的产物不被重写。"""

    def test_only_the_retried_item_is_processed(self, db_session, tmp_path):
        job = _make_job(db_session, tmp_path)
        _settle(db_session, job.id, {1: BackgroundJobItemStatus.FAILED})

        retried = Path(job.items[0].output_path)
        kept = Path(job.items[1].output_path)
        kept_bytes = kept.read_bytes()          # 成功那张的产物

        calls: list[str] = []

        def counting_render(source, page, params, *, max_pixels):
            calls.append(source.name)
            return _fake_render()(source, page, params, max_pixels=max_pixels)

        BackgroundJobService(db_session).retry_item(job.id, 1)
        _make_runner(render_fn=counting_render).run_job(job.id)

        assert calls == ["羊1.png"]                  # 另外两张根本没进算法
        assert retried.is_file()                     # 重试的这张产出了
        assert kept.read_bytes() == kept_bytes       # 成功那张没被重写
        refreshed = _refresh(db_session, job)
        assert refreshed.status == BackgroundJobStatus.SUCCESS
        assert refreshed.completed_images == 3
        assert refreshed.failed_images == 0


