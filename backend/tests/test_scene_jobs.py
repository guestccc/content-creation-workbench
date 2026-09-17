"""智能镜头分割接口测试：覆盖创建、查询、取消、删除与参数校验。

重点是输入校验的边界（相对路径、不存在的目录、输出目录已有旧结果）
和状态机的边界（终态不可取消、未结束不可删除）。

执行层（子进程、进度、结果收集）的测试在 test_scene_runner.py。
"""

import pytest

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

    def test_tilde_path_expanded(self, client, video_dir, tmp_path, monkeypatch):
        """~ 开头的路径应展开成绝对路径。"""
        source, _ = video_dir
        monkeypatch.setenv("HOME", str(tmp_path))
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
        assert clips[0]["thumb_url"].endswith("/clips/1/thumb")

        # 序号越界 → 404（不能借此读到任意文件）
        response = client.get(f"/api/v1/scene/jobs/{created['id']}/clips/99/thumb")
        assert response.status_code == 404, response.text
