"""数据库补列逻辑测试。

`create_all` 只建表不改表，而开发库 workbench.db 里有真实的任务历史不能删。
升级时新增的列必须靠 `_add_missing_columns` 补上，否则一查询就是
`no such column`，整个镜头分割功能直接不可用。

这里不碰 conftest 的内存库 —— 补列要的是「老库」这个场景，所以用临时
文件库手工造一个缺列的表，再把引擎换进去。
"""

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db.base import Base
from app.db import init_db as init_db_module


@pytest.fixture()
def old_db(tmp_path, monkeypatch):
    """造一个「升级前」的库：表结构齐全，但两张表各少几个新列。

    做法是先用当前模型建好表（此时列是齐的），再 DROP 掉那几个新列 ——
    这样表结构、索引、外键都与老库一致，比手写 DDL 更贴近现实。

    - `scene_jobs` 少 `current_phase` / `current_total_clips` / `remark`；
    - `finalcut_copy_jobs` 少 `chars_per_second`（文案任务的语速快照列，
      见 models/finalcut_job.py）。
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        # 先变成「老表结构」，再塞存量数据 —— 反过来的话那些非空新列
        # 会挡住 INSERT，就造不出「老库里有数据」这个场景了。
        conn.execute(text("ALTER TABLE scene_jobs DROP COLUMN current_phase"))
        conn.execute(text("ALTER TABLE scene_jobs DROP COLUMN current_total_clips"))
        # remark 来自 JobRemarkMixin：六个任务表都是后加的，同属「升级要补的列」
        conn.execute(text("ALTER TABLE scene_jobs DROP COLUMN remark"))
        conn.execute(text("ALTER TABLE finalcut_copy_jobs DROP COLUMN chars_per_second"))
        # 塞一行老数据，验证补列不会动存量行
        conn.execute(
            text(
                "INSERT INTO scene_jobs "
                "(mode, status, input_path, output_dir, recursive, params, "
                " total_videos, completed_videos, failed_videos, skipped_videos, "
                " clip_count, scene_count, current_index, current_video, "
                " current_clips, current_clip_names, error_message, "
                " created_at, updated_at) "
                "VALUES ('split', 'success', '/in', '/out', 0, '{}', "
                " 1, 1, 0, 0, 3, 3, 1, 'a.mp4', 3, '[]', '', "
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO finalcut_copy_jobs "
                "(status, subtitle_path, video_path, video_duration, copy_count, "
                " hint, model, raw_response, tokens_used, current_phase, "
                " progress_percent, error_message, created_at, updated_at, remark) "
                "VALUES ('success', '/in.srt', '/in.mp4', 36.294, 5, "
                " '', 'deepseek-chat', '', 0, '', 0, '', "
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00', '')"
            )
        )

    monkeypatch.setattr(init_db_module, "engine", engine)
    return engine


class TestAddMissingColumns:
    """给老库补上模型里新加的列。"""

    def test_adds_the_new_columns(self, old_db):
        """补列后新列可查可写。"""
        added = init_db_module._add_missing_columns()
        assert added == 4

        columns = {col["name"] for col in inspect(old_db).get_columns("scene_jobs")}
        assert {"current_phase", "current_total_clips", "remark"} <= columns
        copy_columns = {
            col["name"] for col in inspect(old_db).get_columns("finalcut_copy_jobs")
        }
        assert "chars_per_second" in copy_columns

    def test_existing_rows_survive_with_defaults(self, old_db):
        """存量行一条不少，新列取到声明的默认值（不然非空约束会把它们顶掉）。"""
        init_db_module._add_missing_columns()

        with old_db.connect() as conn:
            row = conn.execute(
                text("SELECT current_video, current_clips, current_phase, "
                     "current_total_clips, remark FROM scene_jobs")
            ).one()
            copy_row = conn.execute(
                text("SELECT video_duration, chars_per_second FROM finalcut_copy_jobs")
            ).one()
        assert row.current_video == "a.mp4"     # 老数据原样保留
        assert row.current_clips == 3
        assert row.current_phase == ""          # 默认值，不是 NULL
        assert row.current_total_clips == 0
        assert row.remark == ""                 # 老任务没有备注，补成空串
        # 老文案任务：时长还在，语速补成 0.0（= 当时按全局值跑的，值不可考）
        assert copy_row.video_duration == pytest.approx(36.294)
        assert copy_row.chars_per_second == 0.0

    def test_is_idempotent(self, old_db):
        """可以反复执行：第二次一条都不用补，也不会报错。"""
        assert init_db_module._add_missing_columns() == 4
        assert init_db_module._add_missing_columns() == 0

    def test_init_db_runs_the_backfill(self, old_db):
        """init_db 自己要调它 —— 光有函数不调用等于没有。"""
        init_db_module.init_db()

        columns = {col["name"] for col in inspect(old_db).get_columns("scene_jobs")}
        assert {"current_phase", "current_total_clips", "remark"} <= columns
        copy_columns = {
            col["name"] for col in inspect(old_db).get_columns("finalcut_copy_jobs")
        }
        assert "chars_per_second" in copy_columns


class TestRenderDefault:
    """默认值渲染：只认能安全内联的标量，其余宁可跳过。"""

    def test_string_and_number(self):
        from sqlalchemy import Column, Integer, String

        assert init_db_module._render_default(Column(String, default="")) == "''"
        assert init_db_module._render_default(Column(Integer, default=0)) == "0"

    def test_string_with_quote_is_escaped(self):
        from sqlalchemy import Column, String

        col = Column(String, default="it's")
        assert init_db_module._render_default(col) == "'it''s'"

    def test_callable_default_is_refused(self):
        """utcnow 这类可调用默认值渲染不成字面量，猜错了会写坏数据。"""
        from sqlalchemy import Column, DateTime

        from app.models.content import utcnow

        assert init_db_module._render_default(Column(DateTime, default=utcnow)) is None

    def test_sequence_default_is_refused(self):
        """JSON 列的 default=list 同理。"""
        from sqlalchemy import JSON, Column

        assert init_db_module._render_default(Column(JSON, default=list)) is None

    def test_no_default_is_refused(self):
        from sqlalchemy import Column, String

        assert init_db_module._render_default(Column(String)) is None
