"""字幕提取任务的服务层测试。

覆盖：
- 输出文件分配：重名加 -2 / -3 后缀，且**不覆盖**磁盘上已有的同名文件；
- create_job：枚举、参数落库、条目写全、默认输出目录、未装 VideoCaptioner 时拒单；
- 状态机：取消（终态拒取消）、删除（非终态拒删、终态级联删条目、磁盘文件不动）；
- 产物查询：list_subtitles 的存在性语义、get_subtitle_path 的越界保护。
"""

from pathlib import Path

import pytest

from app.core.exceptions import BadRequestError, ConflictError, NotFoundError
from app.core.materials import SUBTITLE, subdir
from app.models.subtitle_job import SubtitleJob, SubtitleJobItem, SubtitleJobStatus
from app.schemas.subtitle_job import SubtitleJobCreate
from app.services.subtitle_env import VcInstall
from app.services.subtitle_job_service import (
    SubtitleJobService,
    allocate_output_paths,
    enumerate_videos,
)


def _make_source(tmp_path, names=("口播A.mp4", "口播B.mp4")) -> Path:
    """造一个含假视频的输入目录。"""
    source = tmp_path / "素材"
    source.mkdir(exist_ok=True)
    for name in names:
        (source / name).write_bytes(b"fake-video")
    return source


def _make_payload(tmp_path, names=("口播A.mp4", "口播B.mp4"), **overrides) -> SubtitleJobCreate:
    """构造一份合法的创建请求。"""
    source = _make_source(tmp_path, names)
    data = {"input_path": str(source), "output_dir": str(tmp_path / "字幕")}
    data.update(overrides)
    return SubtitleJobCreate(**data)


class TestEnumerateVideos:
    """服务层的薄封装：白名单与上限取 SUBTITLE_* 配置。"""

    def test_uses_subtitle_extensions(self, tmp_path):
        source = _make_source(tmp_path, ["a.mp4", "b.srt"])
        videos = enumerate_videos(str(source), recursive=False)
        assert [p.name for p in videos] == ["a.mp4"]


class TestAllocateOutputPaths:
    """输出文件名分配：与镜头分割同一套「找第一个没被占的编号」思路。"""

    def test_plain_names_when_no_conflict(self, tmp_path):
        out = tmp_path / "字幕"
        videos = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
        allocation = allocate_output_paths(videos, out)
        assert allocation[videos[0]] == out / "a.srt"
        assert allocation[videos[1]] == out / "b.srt"

    def test_same_batch_duplicate_stems_get_suffix(self, tmp_path):
        """同批次内重名（recursive 下不同子目录的同名文件）：第二个拿 -2。"""
        out = tmp_path / "字幕"
        videos = [tmp_path / "x" / "口播.mp4", tmp_path / "y" / "口播.mp4"]
        allocation = allocate_output_paths(videos, out)
        assert allocation[videos[0]] == out / "口播.srt"
        assert allocation[videos[1]] == out / "口播-2.srt"

    def test_existing_files_on_disk_are_not_overwritten(self, tmp_path):
        """磁盘上已有同名文件：往后找编号，谁都不覆盖谁。"""
        out = tmp_path / "字幕"
        out.mkdir(parents=True)
        (out / "口播.srt").write_text("已有产物", encoding="utf-8")
        (out / "口播-2.srt").write_text("上一批的", encoding="utf-8")

        videos = [tmp_path / "口播.mp4"]
        allocation = allocate_output_paths(videos, out)
        assert allocation[videos[0]] == out / "口播-3.srt"
        # 原有文件原封不动
        assert (out / "口播.srt").read_text(encoding="utf-8") == "已有产物"

    def test_output_dir_is_precreated(self, tmp_path):
        """目录必须预建：VideoCaptioner 会把不存在的 -o 路径当成文件名。"""
        out = tmp_path / "还没有" / "字幕"
        allocate_output_paths([tmp_path / "a.mp4"], out)
        assert out.is_dir()


class TestCreateJob:
    """创建任务。"""

    def test_creates_job_with_all_items(self, db_session, tmp_path):
        payload = _make_payload(tmp_path, asr="jianying", language="zh")
        job = SubtitleJobService(db_session).create_job(payload)

        assert job.status == SubtitleJobStatus.PENDING
        assert job.total_videos == 2
        assert len(job.items) == 2
        assert [item.index for item in job.items] == [1, 2]
        assert job.items[0].source_name == "口播A.mp4"
        assert job.items[0].output_path.endswith("口播A.srt")
        # 参数原样落库（runner 按白名单从这里取）
        assert job.params == {"asr": "jianying", "language": "zh", "format": "srt"}

    def test_default_output_dir_is_materials_subtitle(self, db_session, tmp_path):
        """不传 output_dir 时用 materials/subtitle 分段。"""
        payload = _make_payload(tmp_path, output_dir=None)
        job = SubtitleJobService(db_session).create_job(payload)
        assert job.output_dir == str(subdir(SUBTITLE))

    def test_default_asr_falls_back_to_bijian(self, db_session, tmp_path):
        payload = _make_payload(tmp_path)
        job = SubtitleJobService(db_session).create_job(payload)
        assert job.params["asr"] == "bijian"

    def test_rejected_when_vc_not_installed(self, db_session, tmp_path, monkeypatch):
        """未装 VideoCaptioner 时拒单 —— 失败要快，不排队。

        conftest 的全局夹具默认伪装成已安装，这个用例覆盖回未安装。
        """
        monkeypatch.setattr(
            "app.services.subtitle_job_service.detect",
            lambda *a, **k: VcInstall(installed=False, detail="没找到"),
        )
        with pytest.raises(BadRequestError, match="VideoCaptioner"):
            SubtitleJobService(db_session).create_job(_make_payload(tmp_path))

    def test_rejected_when_no_videos(self, db_session, tmp_path):
        payload = _make_payload(tmp_path, names=("只有文本.txt",))
        with pytest.raises(BadRequestError, match="没有可处理的视频"):
            SubtitleJobService(db_session).create_job(payload)


class TestCancelAndDelete:
    """状态机：取消与删除。"""

    def test_cancel_pending_job(self, db_session, tmp_path):
        service = SubtitleJobService(db_session)
        job = service.create_job(_make_payload(tmp_path))
        cancelled = service.cancel_job(job.id)
        assert cancelled.status == SubtitleJobStatus.CANCELLED
        assert cancelled.finished_at is not None

    def test_cancel_terminal_job_rejected(self, db_session, tmp_path):
        service = SubtitleJobService(db_session)
        job = service.create_job(_make_payload(tmp_path))
        service.cancel_job(job.id)
        with pytest.raises(ConflictError):
            service.cancel_job(job.id)

    def test_delete_running_job_rejected(self, db_session, tmp_path):
        service = SubtitleJobService(db_session)
        job = service.create_job(_make_payload(tmp_path))
        with pytest.raises(ConflictError, match="尚未结束"):
            service.delete_job(job.id)

    def test_delete_terminal_job_cascades_items(self, db_session, tmp_path):
        """删记录级联删条目，但磁盘上的字幕文件不动。"""
        service = SubtitleJobService(db_session)
        job = service.create_job(_make_payload(tmp_path))
        service.cancel_job(job.id)

        # 假装有一条字幕已产出
        produced = Path(job.items[0].output_path)
        produced.parent.mkdir(parents=True, exist_ok=True)
        produced.write_text("字幕内容", encoding="utf-8")

        service.delete_job(job.id)
        assert db_session.get(SubtitleJob, job.id) is None
        assert db_session.query(SubtitleJobItem).count() == 0
        assert produced.is_file()  # 产物是用户的，删记录不能顺手删文件

    def test_get_missing_job_raises_404(self, db_session):
        with pytest.raises(NotFoundError):
            SubtitleJobService(db_session).get_job(9999)


class TestSubtitlesQuery:
    """产物清单与定位。"""

    def test_list_subtitles_reports_existence(self, db_session, tmp_path):
        """清单如实标注哪些已落盘（列表要展示「该产出哪些、哪些好了」）。"""
        service = SubtitleJobService(db_session)
        job = service.create_job(_make_payload(tmp_path))

        produced = Path(job.items[0].output_path)
        produced.parent.mkdir(parents=True, exist_ok=True)
        produced.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")

        files = service.list_subtitles(job.id)
        assert [f["index"] for f in files] == [1, 2]
        assert files[0]["exists"] is True
        assert files[0]["size_bytes"] > 0
        assert files[1]["exists"] is False

    def test_get_subtitle_path(self, db_session, tmp_path):
        service = SubtitleJobService(db_session)
        job = service.create_job(_make_payload(tmp_path))
        produced = Path(job.items[0].output_path)
        produced.parent.mkdir(parents=True, exist_ok=True)
        produced.write_text("字幕", encoding="utf-8")

        path, name, source_name = service.get_subtitle_path(job.id, 1)
        assert path == produced
        assert name == produced.name
        assert source_name == "口播A.mp4"

    def test_get_subtitle_path_out_of_range(self, db_session, tmp_path):
        service = SubtitleJobService(db_session)
        job = service.create_job(_make_payload(tmp_path))
        with pytest.raises(NotFoundError):
            service.get_subtitle_path(job.id, 99)

    def test_get_subtitle_path_not_generated_yet(self, db_session, tmp_path):
        """序号合法但文件还没生成：404 而不是把路径交出去。"""
        service = SubtitleJobService(db_session)
        job = service.create_job(_make_payload(tmp_path))
        with pytest.raises(NotFoundError, match="还没有生成"):
            service.get_subtitle_path(job.id, 1)
