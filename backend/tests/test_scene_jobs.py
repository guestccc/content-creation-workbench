"""智能镜头分割接口测试：覆盖创建、查询、取消、删除与参数校验。

重点是输入校验的边界（相对路径、不存在的目录、输出目录已有旧结果）
和状态机的边界（终态不可取消、未结束不可删除）。

执行层（子进程、进度、结果收集）的测试在 test_scene_runner.py。
"""

import pytest

from pathlib import Path

from app.models.scene_job import SceneJob, SceneJobItemStatus, SceneJobStatus


@pytest.fixture()
def video_dir(tmp_path):
    """造一个含两条假视频的输入目录，返回 (目录, 视频名列表)。"""
    source = tmp_path / "素材"
    source.mkdir()
    (source / "口播A.mp4").write_bytes(b"fake-video-a")
    (source / "口播B.MOV").write_bytes(b"fake-video-b")
    (source / "说明文档.txt").write_text("不是视频", encoding="utf-8")
    (source / ".隐藏文件.mp4").write_bytes(b"hidden")
    return source, ["口播A.mp4", "口播B.MOV"]


def _create_split_job(client, source, output, **extra) -> dict:
    """创建一个 split 任务并返回响应 data。"""
    payload = {
        "input_path": str(source),
        "output_dir": str(output),
        "mode": "split",
        **extra,
    }
    response = client.post("/api/v1/scene/jobs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


class TestCreateJob:
    """创建任务。"""

    def test_create_split_job_enumerates_videos(self, client, video_dir, tmp_path):
        """目录输入：枚举出全部视频、为每条视频预建独立输出子目录。"""
        source, names = video_dir
        output = tmp_path / "输出"

        data = _create_split_job(client, source, output)

        assert data["status"] == "pending"
        assert data["mode"] == "split"
        assert data["total_videos"] == 2
        assert [item["source_name"] for item in data["items"]] == names
        # 隐藏文件与非视频文件不应被枚举
        assert all(item["source_name"] in names for item in data["items"])
        # 每条视频一个独立输出目录，且创建任务时已经预建
        for item in data["items"]:
            assert item["output_dir"].startswith(str(output))
        # 标准模板的参数应原样落进 params
        assert data["params"]["detector"] == "adaptive"
        assert data["params"]["min_len"] == 0.6

    def test_create_with_file_selection(self, client, video_dir, tmp_path):
        """勾选模式：只处理 files 里点名的文件。"""
        source, _ = video_dir
        data = _create_split_job(client, source, tmp_path / "输出", files=["口播B.MOV"])

        assert data["total_videos"] == 1
        assert data["items"][0]["source_name"] == "口播B.MOV"

    def test_create_single_file_input(self, client, video_dir, tmp_path):
        """单文件输入：只产生一个条目。"""
        source, _ = video_dir
        data = _create_split_job(client, source / "口播A.mp4", tmp_path / "输出")

        assert data["total_videos"] == 1
        assert data["items"][0]["source_name"] == "口播A.mp4"

    def test_create_preview_job_without_output_dir(self, client, video_dir):
        """预览模式不需要输出目录，CSV 写到后端管理的临时目录。"""
        source, _ = video_dir
        response = client.post(
            "/api/v1/scene/jobs",
            json={"input_path": str(source), "mode": "preview"},
        )
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["mode"] == "preview"
        assert "vct-preview" in data["output_dir"]

    def test_split_mode_requires_output_dir(self, client, video_dir):
        """切分模式不给输出目录 → 422。"""
        source, _ = video_dir
        response = client.post(
            "/api/v1/scene/jobs",
            json={"input_path": str(source), "mode": "split"},
        )
        assert response.status_code == 422, response.text

    def test_relative_path_rejected(self, client):
        """相对路径 → 422（后端 cwd 与用户预期不一致，宁可报错也不猜）。"""
        response = client.post(
            "/api/v1/scene/jobs",
            json={"input_path": "videos/input", "output_dir": "/tmp/out", "mode": "split"},
        )
        assert response.status_code == 422, response.text
        assert response.json()["success"] is False

    def test_tilde_path_expanded(self, client, video_dir, tmp_path, fake_home):
        """~ 开头的路径应展开成绝对路径。"""
        source, _ = video_dir
        response = client.post(
            "/api/v1/scene/jobs",
            json={
                "input_path": "~/素材",
                "output_dir": str(tmp_path / "输出"),
                "mode": "split",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["data"]["input_path"] == str(source)

    def test_nonexistent_input_rejected(self, client, tmp_path):
        """输入路径不存在 → 400。"""
        response = client.post(
            "/api/v1/scene/jobs",
            json={
                "input_path": str(tmp_path / "不存在"),
                "output_dir": str(tmp_path / "输出"),
                "mode": "split",
            },
        )
        assert response.status_code == 400, response.text

    def test_empty_directory_rejected(self, client, tmp_path):
        """目录里没有视频 → 400。"""
        empty = tmp_path / "空目录"
        empty.mkdir()
        response = client.post(
            "/api/v1/scene/jobs",
            json={
                "input_path": str(empty),
                "output_dir": str(tmp_path / "输出"),
                "mode": "split",
            },
        )
        assert response.status_code == 400, response.text

    def test_existing_clips_in_output_rejected(self, client, video_dir, tmp_path):
        """输出目录已有同名切割结果 → 400，防止历史残留污染进度计数。"""
        source, _ = video_dir
        output = tmp_path / "输出"
        stale = output / "口播A_scenes"
        stale.mkdir(parents=True)
        (stale / "口播A_clip_001.mp4").write_bytes(b"old")

        response = client.post(
            "/api/v1/scene/jobs",
            json={"input_path": str(source), "output_dir": str(output), "mode": "split"},
        )
        assert response.status_code == 400, response.text
        assert "已存在同名切割结果" in response.text

    @pytest.mark.parametrize(
        "field,value",
        [
            ("template", "不存在的模板"),
            ("detector", "不存在的检测器"),
            ("mode", "不存在的模式"),
            ("threshold", 0.01),
            ("threshold", 500.0),
            ("min_len", 0.01),
        ],
    )
    def test_invalid_params_rejected(self, client, video_dir, tmp_path, field, value):
        """非法模板/检测器/模式/越界阈值 → 422。"""
        source, _ = video_dir
        payload = {
            "input_path": str(source),
            "output_dir": str(tmp_path / "输出"),
            "mode": "split",
            field: value,
        }
        response = client.post("/api/v1/scene/jobs", json=payload)
        assert response.status_code == 422, response.text

    def test_files_with_path_separator_rejected(self, client, video_dir, tmp_path):
        """勾选文件名带路径分隔符 → 422（防越出输入目录）。"""
        source, _ = video_dir
        response = client.post(
            "/api/v1/scene/jobs",
            json={
                "input_path": str(source),
                "output_dir": str(tmp_path / "输出"),
                "mode": "split",
                "files": ["../越界.mp4"],
            },
        )
        assert response.status_code == 422, response.text

    def test_missing_selected_file_rejected(self, client, video_dir, tmp_path):
        """勾选的文件在目录里不存在 → 400。"""
        source, _ = video_dir
        response = client.post(
            "/api/v1/scene/jobs",
            json={
                "input_path": str(source),
                "output_dir": str(tmp_path / "输出"),
                "mode": "split",
                "files": ["不存在.mp4"],
            },
        )
        assert response.status_code == 400, response.text


class TestQueryJob:
    """查询与列表。"""

    def test_get_job(self, client, video_dir, tmp_path):
        """按 ID 查询应返回任务与每个视频的明细。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")

        response = client.get(f"/api/v1/scene/jobs/{created['id']}")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["id"] == created["id"]
        assert len(data["items"]) == 2
        assert data["progress_percent"] == 0

    def test_get_missing_job_returns_404(self, client):
        """任务不存在 → 404。"""
        response = client.get("/api/v1/scene/jobs/99999")
        assert response.status_code == 404, response.text

    def test_list_jobs_without_items(self, client, video_dir, tmp_path):
        """列表接口不携带每个视频的明细，避免响应体过大。"""
        source, _ = video_dir
        _create_split_job(client, source, tmp_path / "输出")

        response = client.get("/api/v1/scene/jobs")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["items"] == []

    def test_list_jobs_filter_by_status(self, client, video_dir, tmp_path):
        """按状态过滤：不存在的状态应返回空列表而不是全部。"""
        source, _ = video_dir
        _create_split_job(client, source, tmp_path / "输出")

        response = client.get("/api/v1/scene/jobs", params={"status": "success"})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["total"] == 0

        response = client.get("/api/v1/scene/jobs", params={"status": "pending"})
        assert response.json()["data"]["total"] == 1


class TestCancelAndDelete:
    """取消与删除的状态机边界。"""

    def test_cancel_pending_job(self, client, video_dir, tmp_path):
        """取消排队中的任务 → cancelled。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")

        response = client.post(f"/api/v1/scene/jobs/{created['id']}/cancel")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == SceneJobStatus.CANCELLED

    def test_cancel_terminal_job_conflict(self, client, video_dir, tmp_path):
        """已取消的任务再取消 → 409。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")
        client.post(f"/api/v1/scene/jobs/{created['id']}/cancel")

        response = client.post(f"/api/v1/scene/jobs/{created['id']}/cancel")
        assert response.status_code == 409, response.text

    def test_cancel_missing_job_returns_404(self, client):
        """取消不存在的任务 → 404。"""
        response = client.post("/api/v1/scene/jobs/99999/cancel")
        assert response.status_code == 404, response.text

    def test_delete_pending_job_conflict(self, client, video_dir, tmp_path):
        """未结束的任务不允许删除 → 409。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")

        response = client.delete(f"/api/v1/scene/jobs/{created['id']}")
        assert response.status_code == 409, response.text

    def test_delete_terminal_job_cascades(self, client, video_dir, tmp_path, db_session):
        """取消后可删除，条目级联删除。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")
        client.post(f"/api/v1/scene/jobs/{created['id']}/cancel")

        response = client.delete(f"/api/v1/scene/jobs/{created['id']}")
        assert response.status_code == 200, response.text
        assert db_session.get(SceneJob, created["id"]) is None


class TestRetryItem:
    """重试单条失败视频：状态机边界与输出目录清理。"""

    @staticmethod
    def _finish_with_one_failure(db_session, job_id: int) -> None:
        """把任务手工推到 partial：第一条成功、第二条失败，并留残留产物。"""
        job = db_session.get(SceneJob, job_id)
        first, second = job.items[0], job.items[1]
        first.status = SceneJobItemStatus.SUCCESS
        first.clip_count = 2
        first.clip_names = ["a_clip_001.mp4", "a_clip_002.mp4"]
        second.status = SceneJobItemStatus.FAILED
        second.error_message = "输入文件不存在"
        # 失败条目的输出目录里塞一个残留片段 + 旧日志，重试时应当被清掉
        stale_dir = Path(second.output_dir)
        stale_dir.mkdir(parents=True, exist_ok=True)
        (stale_dir / "b_clip_001.mp4").write_bytes(b"stale")
        (stale_dir / "vct.log").write_text("old log", encoding="utf-8")
        job.status = SceneJobStatus.PARTIAL
        job.completed_videos = 2
        job.failed_videos = 1
        job.error_message = "第二条失败"
        db_session.commit()

    def test_retry_failed_item_requeues_job(self, client, video_dir, tmp_path, db_session):
        """失败的条目重置回 pending、任务重新入队，成功条目与计数口径正确。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")
        self._finish_with_one_failure(db_session, created["id"])

        response = client.post(f"/api/v1/scene/jobs/{created['id']}/items/2/retry")
        assert response.status_code == 200, response.text
        data = response.json()["data"]

        assert data["status"] == "pending"
        assert data["error_message"] == ""
        # completed 只数成功的（重试的那条回到未完成）
        assert data["completed_videos"] == 1
        assert data["failed_videos"] == 0
        items = {item["index"]: item for item in data["items"]}
        assert items[1]["status"] == "success"  # 成功条目原样保留
        assert items[2]["status"] == "pending"
        assert items[2]["error_message"] == ""
        assert items[2]["clip_count"] == 0

        # 残留产物被清掉：进度与结果统计靠目录里的文件，不清就会污染重跑
        stale_dir = Path(items[2]["output_dir"])
        assert list(stale_dir.iterdir()) == []

    def test_retry_rejects_non_failed_item(self, client, video_dir, tmp_path, db_session):
        """成功条目不可重试。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")
        self._finish_with_one_failure(db_session, created["id"])

        response = client.post(f"/api/v1/scene/jobs/{created['id']}/items/1/retry")
        assert response.status_code == 409, response.text

    def test_retry_rejects_running_job(self, client, video_dir, tmp_path, db_session):
        """任务没结束（running）时不可重试。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")
        job = db_session.get(SceneJob, created["id"])
        job.status = SceneJobStatus.RUNNING
        job.items[1].status = SceneJobItemStatus.FAILED
        db_session.commit()

        response = client.post(f"/api/v1/scene/jobs/{created['id']}/items/2/retry")
        assert response.status_code == 409, response.text

    def test_retry_unknown_item_404(self, client, video_dir, tmp_path, db_session):
        """条目序号越界 → 404。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")
        self._finish_with_one_failure(db_session, created["id"])

        response = client.post(f"/api/v1/scene/jobs/{created['id']}/items/99/retry")
        assert response.status_code == 404, response.text


class TestTemplatesAndEnvironment:
    """模板与环境自检接口。"""

    def test_templates_include_custom_and_one_recommended(self, client):
        """模板列表：5 个预设 + 1 个自定义；推荐模板有且仅有一个。"""
        response = client.get("/api/v1/scene/templates")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert len(data) == 6
        assert data[-1]["key"] == "custom"
        assert sum(1 for item in data if item["recommended"]) == 1
        # 阈值文案由后端拼好，前端不做逻辑
        standard = next(item for item in data if item["key"] == "standard")
        assert "默认" in standard["threshold_label"]

    def test_environment_reports_dependencies(self, client):
        """环境自检：返回 ready 标记与各依赖明细，结构完整。"""
        response = client.get("/api/v1/scene/environment")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert "ready" in data
        assert "vct_path" in data
        # 素材目录随自检一起回给前端：根目录用于提示，两个默认值决定页面
        # 打开时输入/输出框停在哪儿（source/ → clips/），顺路带回省两次请求
        assert data["materials_dir"]
        assert data["default_input_dir"].endswith("/source")
        assert data["default_output_dir"].endswith("/clips")
        assert data["default_input_dir"].startswith(data["materials_dir"])
        assert data["default_output_dir"].startswith(data["materials_dir"])
        names = {dep["name"] for dep in data["dependencies"]}
        assert names == {"vct", "scenedetect", "ffmpeg", "ffprobe"}


class TestScenesAndClips:
    """切点汇总与片段列表。"""

    def test_scenes_summary_of_fresh_job(self, client, video_dir, tmp_path):
        """新任务的切点汇总：全零但不报错。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")

        response = client.get(f"/api/v1/scene/jobs/{created['id']}/scenes")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["total_scenes"] == 0
        assert data["shortest"] is None
        assert len(data["items"]) == 2

    def test_scenes_summary_missing_job_404(self, client):
        """任务不存在 → 404。"""
        response = client.get("/api/v1/scene/jobs/99999/scenes")
        assert response.status_code == 404, response.text

    def test_list_clips_and_thumb_404(self, client, video_dir, tmp_path, db_session):
        """片段列表按任务记录推导；序号越界的缩略图请求 → 404。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")

        # 手工给第一个条目录入两个片段名并造出文件
        job = db_session.get(SceneJob, created["id"])
        item = job.items[0]
        item.clip_names = ["口播A_clip_001.mp4", "口播A_clip_002.mp4"]
        item.clip_count = 2
        item.status = SceneJobItemStatus.SUCCESS
        db_session.commit()
        out = tmp_path / "片段输出"
        for name in item.clip_names:
            (out / name).parent.mkdir(parents=True, exist_ok=True)
            (out / name).write_bytes(b"fake")
        # 把条目输出目录指到造好的目录
        item.output_dir = str(out)
        db_session.commit()

        response = client.get(f"/api/v1/scene/jobs/{created['id']}/clips")
        assert response.status_code == 200, response.text
        clips = response.json()["data"]
        assert len(clips) == 2
        assert clips[0]["index"] == 1
        assert clips[0]["item_index"] == 1
        assert clips[0]["thumb_url"].endswith("/clips/1/thumb")

        # 序号越界 → 404（不能借此读到任意文件）
        response = client.get(f"/api/v1/scene/jobs/{created['id']}/clips/99/thumb")
        assert response.status_code == 404, response.text

    def test_clips_carry_owning_item_index(self, client, tmp_path, db_session):
        """每个片段要知道自己是哪条视频切出来的 —— 前端按它归组。

        这里故意造两条**同名**的视频（放在不同的子目录里，递归扫描）：
        按文件名归组必然把它们的片段混在一起，`item_index` 才是可信的归属。
        序号（index）仍是全任务连续的，缩略图/播放接口靠它定位。
        """
        source = tmp_path / "素材"
        for sub in ("甲", "乙"):
            (source / sub).mkdir(parents=True)
            (source / sub / "同名.mp4").write_bytes(b"fake")
        created = _create_split_job(client, source, tmp_path / "输出", recursive=True)

        job = db_session.get(SceneJob, created["id"])
        assert [item.source_name for item in job.items] == ["同名.mp4", "同名.mp4"]
        for item in job.items:
            item.clip_names = ["同名_clip_001.mp4", "同名_clip_002.mp4"]
            item.clip_count = 2
            item.status = SceneJobItemStatus.SUCCESS
            out = tmp_path / "片段输出" / str(item.index)
            out.mkdir(parents=True)
            for name in item.clip_names:
                (out / name).write_bytes(b"fake")
            item.output_dir = str(out)
        db_session.commit()

        response = client.get(f"/api/v1/scene/jobs/{created['id']}/clips")
        assert response.status_code == 200, response.text
        clips = response.json()["data"]
        assert [clip["index"] for clip in clips] == [1, 2, 3, 4]
        assert [clip["item_index"] for clip in clips] == [1, 1, 2, 2]


class TestClipVideo:
    """片段在线播放：整文件 200 / 字节段 206 / 越界 416 / 找不到 404。

    Range 解析本身的穷举在 test_file_range.py，这里钉的是「接口把这些
    行为真的暴露出来了」，以及字节内容一字不差 —— 这才是浏览器拖进度条
    依赖的东西。
    """

    #: 内容可预测的片段：第 i 个字节就是 i（取模），方便核对字节段
    CLIP_BYTES = bytes(i % 256 for i in range(1000))

    @pytest.fixture()
    def clip_url(self, client, video_dir, tmp_path, db_session) -> str:
        """造一个带一个真实片段文件的任务，返回它的 video 接口地址。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")

        job = db_session.get(SceneJob, created["id"])
        item = job.items[0]
        item.clip_names = ["口播A_clip_001.mp4"]
        item.clip_count = 1
        item.status = SceneJobItemStatus.SUCCESS
        out = tmp_path / "片段输出"
        out.mkdir()
        (out / "口播A_clip_001.mp4").write_bytes(self.CLIP_BYTES)
        item.output_dir = str(out)
        db_session.commit()

        return f"/api/v1/scene/jobs/{created['id']}/clips/1/video"

    def test_full_download(self, client, clip_url):
        """不带 Range：200 + 整文件 + 声明支持分段。"""
        response = client.get(clip_url)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["accept-ranges"] == "bytes"
        assert int(response.headers["content-length"]) == len(self.CLIP_BYTES)
        assert response.content == self.CLIP_BYTES

    def test_partial_content(self, client, clip_url):
        """bytes=100-199 → 206，Content-Range 与字节内容都要对。"""
        response = client.get(clip_url, headers={"Range": "bytes=100-199"})
        assert response.status_code == 206, response.text
        assert response.headers["content-range"] == "bytes 100-199/1000"
        assert response.headers["content-length"] == "100"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.content == self.CLIP_BYTES[100:200]

    def test_open_ended_range(self, client, clip_url):
        """bytes=0-：浏览器起手式。"""
        response = client.get(clip_url, headers={"Range": "bytes=0-"})
        assert response.status_code == 206
        assert response.headers["content-range"] == "bytes 0-999/1000"
        assert response.content == self.CLIP_BYTES

    def test_suffix_range(self, client, clip_url):
        response = client.get(clip_url, headers={"Range": "bytes=-10"})
        assert response.status_code == 206
        assert response.headers["content-range"] == "bytes 990-999/1000"
        assert response.content == self.CLIP_BYTES[-10:]

    def test_seek_to_end_then_middle(self, client, clip_url):
        """模拟拖动：先取末尾（预载 moov），再取中段。"""
        tail = client.get(clip_url, headers={"Range": "bytes=980-"})
        assert tail.status_code == 206
        assert tail.content == self.CLIP_BYTES[980:]

        middle = client.get(clip_url, headers={"Range": "bytes=400-499"})
        assert middle.status_code == 206
        assert middle.content == self.CLIP_BYTES[400:500]

    def test_unsatisfiable_range_is_416(self, client, clip_url):
        response = client.get(clip_url, headers={"Range": "bytes=1000-"})
        assert response.status_code == 416
        assert response.headers["content-range"] == "bytes */1000"

    def test_clip_not_found_is_404(self, client, video_dir, tmp_path):
        """序号越界 → 404，不能借此读到任意文件。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")
        response = client.get(f"/api/v1/scene/jobs/{created['id']}/clips/99/video")
        assert response.status_code == 404, response.text

    def test_job_not_found_is_404(self, client):
        response = client.get("/api/v1/scene/jobs/99999/clips/1/video")
        assert response.status_code == 404, response.text

    def test_clips_list_carries_video_url(self, client, clip_url, video_dir, tmp_path, db_session):
        """片段列表要把 video_url 带回来，前端不该自己拼路径。"""
        # clip_url 里有 job id，解析出来再调列表接口
        job_id = clip_url.split("/jobs/")[1].split("/")[0]
        response = client.get(f"/api/v1/scene/jobs/{job_id}/clips")
        assert response.status_code == 200, response.text
        clips = response.json()["data"]
        assert clips[0]["video_url"].endswith("/clips/1/video")


class TestBatchDelete:
    """批量删除：整批成功或整批失败（POST /jobs/batch-delete）。"""

    def _create_terminal(self, client, video_dir, tmp_path, tag) -> int:
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / f"输出{tag}")
        client.post(f"/api/v1/scene/jobs/{created['id']}/cancel")
        return created["id"]

    def test_batch_delete_success(self, client, video_dir, tmp_path, db_session):
        ids = [self._create_terminal(client, video_dir, tmp_path, tag) for tag in "abc"]

        response = client.post("/api/v1/scene/jobs/batch-delete", json={"ids": ids})
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"ids": ids, "count": 3}
        for job_id in ids:
            assert db_session.get(SceneJob, job_id) is None

    def test_batch_delete_non_terminal_conflict_rolls_back(
        self, client, video_dir, tmp_path, db_session
    ):
        """批里混着一条未结束的：整批 409，已勾选的终态任务一条也不能被删。"""
        ids = [self._create_terminal(client, video_dir, tmp_path, tag) for tag in "ab"]
        source, _ = video_dir
        pending = _create_split_job(client, source, tmp_path / "输出c")["id"]

        response = client.post(
            "/api/v1/scene/jobs/batch-delete", json={"ids": [*ids, pending]}
        )
        assert response.status_code == 409, response.text
        assert f"#{pending}" in response.json()["error"]["message"]
        for job_id in [*ids, pending]:
            assert db_session.get(SceneJob, job_id) is not None

    def test_batch_delete_missing_id_rolls_back(
        self, client, video_dir, tmp_path, db_session
    ):
        job_id = self._create_terminal(client, video_dir, tmp_path, "a")

        response = client.post(
            "/api/v1/scene/jobs/batch-delete", json={"ids": [job_id, 99999]}
        )
        assert response.status_code == 404, response.text
        assert db_session.get(SceneJob, job_id) is not None

    def test_batch_delete_empty_ids_422(self, client):
        response = client.post("/api/v1/scene/jobs/batch-delete", json={"ids": []})
        assert response.status_code == 422, response.text

    def test_batch_delete_over_limit_422(self, client):
        response = client.post(
            "/api/v1/scene/jobs/batch-delete", json={"ids": list(range(1, 102))}
        )
        assert response.status_code == 422, response.text

    def test_batch_delete_dedupes_ids(self, client, video_dir, tmp_path):
        ids = [self._create_terminal(client, video_dir, tmp_path, tag) for tag in "ab"]

        response = client.post(
            "/api/v1/scene/jobs/batch-delete", json={"ids": [ids[0], ids[0], ids[1]]}
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"ids": ids, "count": 2}


class TestDeletePurge:
    """删除任务时可选连产物一起清（purge_files）。"""

    def _terminal_with_products(self, client, video_dir, tmp_path, tag) -> tuple[int, list[Path]]:
        """造一条终态任务，并在每条视频的输出目录里放一个假片段。

        Returns:
            (任务 ID, 产物目录清单)
        """
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / f"输出{tag}")
        dirs = []
        for item in created["items"]:
            out_dir = Path(item["output_dir"])
            (out_dir / "口播A_scene-1_clip_001.mp4").write_bytes(b"fake-clip")
            dirs.append(out_dir)
        client.post(f"/api/v1/scene/jobs/{created['id']}/cancel")
        return created["id"], dirs

    def test_delete_without_purge_keeps_products(self, client, video_dir, tmp_path):
        """默认只删记录：磁盘上的片段文件原样保留。"""
        job_id, dirs = self._terminal_with_products(client, video_dir, tmp_path, "a")

        response = client.delete(f"/api/v1/scene/jobs/{job_id}")
        assert response.status_code == 200, response.text
        for out_dir in dirs:
            assert list(out_dir.glob("*_clip_*.mp4")), f"产物被误删：{out_dir}"

    def test_delete_with_purge_removes_products(self, client, video_dir, tmp_path):
        """purge_files=true：输出目录整棵删掉。"""
        job_id, dirs = self._terminal_with_products(client, video_dir, tmp_path, "b")

        response = client.delete(f"/api/v1/scene/jobs/{job_id}?purge_files=true")
        assert response.status_code == 200, response.text
        for out_dir in dirs:
            assert not out_dir.exists(), f"产物目录没清掉：{out_dir}"

    def test_batch_delete_with_purge_removes_products(self, client, video_dir, tmp_path):
        """批量删除同样吃 purge_files：两条任务的产物都要清掉。"""
        first_id, first_dirs = self._terminal_with_products(client, video_dir, tmp_path, "c")
        second_id, second_dirs = self._terminal_with_products(client, video_dir, tmp_path, "d")

        response = client.post(
            "/api/v1/scene/jobs/batch-delete",
            json={"ids": [first_id, second_id], "purge_files": True},
        )
        assert response.status_code == 200, response.text
        for out_dir in [*first_dirs, *second_dirs]:
            assert not out_dir.exists(), f"产物目录没清掉：{out_dir}"

    def test_batch_delete_without_purge_keeps_products(self, client, video_dir, tmp_path):
        """批量删除不勾选时，产物一条都不许动。"""
        job_id, dirs = self._terminal_with_products(client, video_dir, tmp_path, "e")

        response = client.post(
            "/api/v1/scene/jobs/batch-delete", json={"ids": [job_id]}
        )
        assert response.status_code == 200, response.text
        for out_dir in dirs:
            assert out_dir.is_dir(), f"产物被误删：{out_dir}"


class TestUpdateRemark:
    """更新任务备注（PUT /jobs/{id}/remark）：纯用户标记，不参与状态机。"""

    def test_remark_echoed_in_detail_and_list(self, client, video_dir, tmp_path):
        """保存成功：详情接口与列表接口都回显新备注。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")
        remark = "给客户 A 的那版"

        response = client.put(
            f"/api/v1/scene/jobs/{created['id']}/remark", json={"remark": remark}
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["remark"] == remark

        detail = client.get(f"/api/v1/scene/jobs/{created['id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["remark"] == remark

        # 列表走的是 from_model(include_items=False) 那条分支，容易漏掉 remark
        listed = client.get("/api/v1/scene/jobs")
        assert listed.status_code == 200, listed.text
        assert listed.json()["data"]["items"][0]["remark"] == remark

    def test_empty_remark_clears_existing(self, client, video_dir, tmp_path):
        """空串是有效值：把已有备注清掉，而不是被当成「不更新」。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")
        url = f"/api/v1/scene/jobs/{created['id']}/remark"
        client.put(url, json={"remark": "先写一句"})

        response = client.put(url, json={"remark": ""})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["remark"] == ""

        detail = client.get(f"/api/v1/scene/jobs/{created['id']}")
        assert detail.json()["data"]["remark"] == ""

    def test_remark_over_limit_422(self, client, video_dir, tmp_path):
        """超过 200 字 → 422（上限与前端编辑弹窗的 maxLength 一致）。"""
        source, _ = video_dir
        created = _create_split_job(client, source, tmp_path / "输出")

        response = client.put(
            f"/api/v1/scene/jobs/{created['id']}/remark", json={"remark": "备" * 201}
        )
        assert response.status_code == 422, response.text
        assert response.json()["success"] is False

    def test_update_remark_missing_job_returns_404(self, client):
        """任务不存在 → 404。"""
        response = client.put("/api/v1/scene/jobs/99999/remark", json={"remark": "x"})
        assert response.status_code == 404, response.text
