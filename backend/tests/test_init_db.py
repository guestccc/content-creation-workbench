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
    """造一个「升级前」的库：表结构齐全，但有三张表各少几个新列。

    做法是先用当前模型建好表（此时列是齐的），再 DROP 掉那几个新列 ——
    这样表结构、索引、外键都与老库一致，比手写 DDL 更贴近现实。

    - `scene_jobs` 少 `current_phase` / `current_total_clips` / `remark`；
    - `finalcut_copy_jobs` 少 `chars_per_second`（文案任务的语速快照列，
      见 models/finalcut_job.py）；
    - `background_jobs` 少 `source_crawl_job_id` / `source_crawl_note_id`
      （换背景任务的来源列，见 models/background_job.py）；
    - `crawl_note_ai_copies` 少 `reasoning`（AI 文案的思维链列，
      见 models/crawl_ai_copy.py）；
    - `crawl_jobs` 少 `source_crawl_job_id` / `source_crawl_note_id`（评论补抓的
      派生来源列，见 models/crawl_job.py）以及那条联合索引。
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
        conn.execute(text("ALTER TABLE background_jobs DROP COLUMN source_crawl_job_id"))
        conn.execute(text("ALTER TABLE background_jobs DROP COLUMN source_crawl_note_id"))
        conn.execute(text("ALTER TABLE crawl_note_ai_copies DROP COLUMN reasoning"))
        # SQLite 不允许 DROP 掉被索引引用的列，老库本来也没有这条索引，先删索引
        conn.execute(text("DROP INDEX ix_crawl_jobs_source"))
        conn.execute(text("ALTER TABLE crawl_jobs DROP COLUMN source_crawl_job_id"))
        conn.execute(text("ALTER TABLE crawl_jobs DROP COLUMN source_crawl_note_id"))
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
        conn.execute(
            text(
                "INSERT INTO background_jobs "
                "(status, input_path, output_dir, files, background_path, params, "
                " total_images, completed_images, failed_images, skipped_images, "
                " current_index, current_image, current_elapsed_seconds, "
                " error_message, remark, created_at, updated_at) "
                "VALUES ('success', '/in', '/out', '[]', '/bg.png', '{}', "
                " 3, 3, 0, 0, 3, 'c.png', 1, '', '', "
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        conn.execute(
            text(
                # 非空的列都要显式给值：Python 侧的 default 不在 DDL 里
                "INSERT INTO crawl_jobs "
                "(status, platform, crawler_type, login_type, params, login_cookies, "
                " expected_count, crawled_count, note_count, elapsed_seconds, "
                " error_message, remark, created_at, updated_at) "
                "VALUES ('success', 'xhs', 'search', 'qrcode', '{}', '', "
                " 1, 1, 1, 3, '', '', "
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO crawl_note_ai_copies "
                "(crawl_job_id, note_id, payload, model, raw_response, tokens_used, "
                " created_at, updated_at) "
                "VALUES (1, 'n1', '{\"xhs\": {\"titles\": [], \"intros\": []}, "
                " \"dy\": {\"titles\": [], \"intros\": []}}', 'deepseek-chat', '', 7, "
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )

    monkeypatch.setattr(init_db_module, "engine", engine)
    return engine


class TestAddMissingColumns:
    """给老库补上模型里新加的列。"""

    def test_adds_the_new_columns(self, old_db):
        """补列后新列可查可写。"""
        added = init_db_module._add_missing_columns()
        assert added == 9

        columns = {col["name"] for col in inspect(old_db).get_columns("scene_jobs")}
        assert {"current_phase", "current_total_clips", "remark"} <= columns
        copy_columns = {
            col["name"] for col in inspect(old_db).get_columns("finalcut_copy_jobs")
        }
        assert "chars_per_second" in copy_columns
        background_columns = {
            col["name"] for col in inspect(old_db).get_columns("background_jobs")
        }
        assert {"source_crawl_job_id", "source_crawl_note_id"} <= background_columns
        ai_copy_columns = {
            col["name"] for col in inspect(old_db).get_columns("crawl_note_ai_copies")
        }
        assert "reasoning" in ai_copy_columns
        crawl_columns = {
            col["name"] for col in inspect(old_db).get_columns("crawl_jobs")
        }
        assert {"source_crawl_job_id", "source_crawl_note_id"} <= crawl_columns

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
            background_row = conn.execute(
                text("SELECT total_images, source_crawl_job_id, source_crawl_note_id "
                     "FROM background_jobs")
            ).one()
            ai_copy_row = conn.execute(
                text("SELECT payload, tokens_used, reasoning "
                     "FROM crawl_note_ai_copies")
            ).one()
            crawl_row = conn.execute(
                text("SELECT note_count, source_crawl_job_id, source_crawl_note_id "
                     "FROM crawl_jobs")
            ).one()
        assert row.current_video == "a.mp4"     # 老数据原样保留
        assert row.current_clips == 3
        assert row.current_phase == ""          # 默认值，不是 NULL
        assert row.current_total_clips == 0
        assert row.remark == ""                 # 老任务没有备注，补成空串
        # 老文案任务：时长还在，语速补成 0.0（= 当时按全局值跑的，值不可考）
        assert copy_row.video_duration == pytest.approx(36.294)
        assert copy_row.chars_per_second == 0.0
        # 老换背景任务：来源两列补出来 —— 可空的那列是 NULL，非空的那列靠
        # 内联的 DEFAULT '' 落地。后者正是「默认值必须写成标量」要守的东西：
        # 换成 default_factory 之类渲染不出来的默认值，这一行会直接 INSERT 失败。
        assert background_row.total_images == 3
        assert background_row.source_crawl_job_id is None
        assert background_row.source_crawl_note_id == ""
        # 老 AI 文案行：payload 与 tokens 原样，思维链补成空串（那时还没这一列）
        assert ai_copy_row.tokens_used == 7
        assert '"xhs"' in ai_copy_row.payload
        assert ai_copy_row.reasoning == ""
        # 老抓取任务：来源两列补出来 —— 可空的 NULL，非空的那列靠内联 DEFAULT ''
        assert crawl_row.note_count == 1
        assert crawl_row.source_crawl_job_id is None
        assert crawl_row.source_crawl_note_id == ""

    def test_a_row_can_be_inserted_after_the_backfill(self, old_db):
        """补完列后还能插新行 —— 证明 DEFAULT '' 真的进了表定义。

        只把列加上、却没带 DEFAULT 的话，补列本身会失败（SQLite 拒收非空无默认
        的 ADD COLUMN），但这里是从「补列之后写入」这一侧再钉一遍。
        """
        init_db_module._add_missing_columns()

        with old_db.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO background_jobs "
                    "(status, input_path, output_dir, files, background_path, params, "
                    " total_images, completed_images, failed_images, skipped_images, "
                    " current_index, current_image, current_elapsed_seconds, "
                    " error_message, remark, created_at, updated_at) "
                    "VALUES ('pending', '/in2', '/out2', '[]', '/bg2.png', '{}', "
                    " 1, 0, 0, 0, 0, '', 0, '', '', "
                    " '2026-01-02 00:00:00', '2026-01-02 00:00:00')"
                )
            )
            row = conn.execute(
                text("SELECT source_crawl_job_id, source_crawl_note_id "
                     "FROM background_jobs ORDER BY id DESC LIMIT 1")
            ).one()
        assert row.source_crawl_job_id is None
        assert row.source_crawl_note_id == ""

    def test_is_idempotent(self, old_db):
        """可以反复执行：第二次一条都不用补，也不会报错。"""
        assert init_db_module._add_missing_columns() == 9
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
        background_columns = {
            col["name"] for col in inspect(old_db).get_columns("background_jobs")
        }
        assert {"source_crawl_job_id", "source_crawl_note_id"} <= background_columns
        ai_copy_columns = {
            col["name"] for col in inspect(old_db).get_columns("crawl_note_ai_copies")
        }
        assert "reasoning" in ai_copy_columns
        crawl_columns = {
            col["name"] for col in inspect(old_db).get_columns("crawl_jobs")
        }
        assert {"source_crawl_job_id", "source_crawl_note_id"} <= crawl_columns


class TestAddMissingIndexes:
    """给老库补上模型里新加的索引。

    成因与补列相同但更隐蔽：`create_all` 对已存在的表整个跳过，连带
    `__table_args__` 里新加的索引一起跳过 —— 不补的话，代码里写着「这里加了
    索引」，用户现有的 workbench.db 上却并不存在。
    """

    def test_adds_the_missing_index(self, old_db):
        """ix_crawl_jobs_source 补回来，且列顺序与模型声明一致。

        索引引用的是补出来的新列，所以得按 init_db 的顺序先补列再补索引 ——
        光补索引会因为列不存在而失败（按设计只告警跳过，见 _add_missing_indexes）。
        """
        init_db_module._add_missing_columns()
        added = init_db_module._add_missing_indexes()
        assert added == 1

        indexes = {
            index["name"]: index["column_names"]
            for index in inspect(old_db).get_indexes("crawl_jobs")
        }
        assert indexes["ix_crawl_jobs_source"] == [
            "source_crawl_job_id",
            "source_crawl_note_id",
        ]

    def test_is_idempotent(self, old_db):
        init_db_module._add_missing_columns()
        assert init_db_module._add_missing_indexes() == 1
        assert init_db_module._add_missing_indexes() == 0

    def test_init_db_runs_the_backfill(self, old_db):
        """init_db 自己要调它 —— 光有函数不调用等于没有。"""
        init_db_module.init_db()

        names = {index["name"] for index in inspect(old_db).get_indexes("crawl_jobs")}
        assert "ix_crawl_jobs_source" in names

    def test_works_together_with_the_column_backfill(self, old_db):
        """先补列再补索引（init_db 的顺序）：索引引用的是刚补出来的列。"""
        init_db_module.init_db()

        columns = {col["name"] for col in inspect(old_db).get_columns("crawl_jobs")}
        indexes = {index["name"] for index in inspect(old_db).get_indexes("crawl_jobs")}
        assert {"source_crawl_job_id", "source_crawl_note_id"} <= columns
        assert "ix_crawl_jobs_source" in indexes


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
