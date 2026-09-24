"""素材抓取执行层（crawl_runner.py）测试。

与 test_subtitle_runner.py 同一个套路：FakeMcPopen 注入、tick/progress 都设 0，
整个执行流程在主线程同步跑完 —— 不起线程、不起真子进程、不需要真爬虫。

这里额外钉住几条「素材抓取独有」的判断（实现注释里写明的，改了就坏）：
- 结果以产物为准：退出码非 0 但 jsonl 有内容判成功；退出码 0 但一条没抓到判失败；
- 取消≠清零：running 中取消，已抓到的 jsonl 计入 note_count；
- launcher 缺失 / 队列被取消认领时，任务必须落到明确终态而不是卡在 running。
"""

import json
import time
from pathlib import Path

import pytest

from app.models.crawl_job import CrawlJob, CrawlJobStatus
from app.services import media_tools
from app.schemas.crawl_job import CrawlJobCreate
from app.services.crawl_job_service import CrawlJobService, estimate_expected_count
from app.services.crawl_job_worker import CrawlJobWorker
from app.services.crawler_env import McInstall
from app.services.crawl_runner import (
    CrawlRunner,
    build_argv,
    claim_next_pending_id,
    detect_phase,
    is_our_child,
    recover_interrupted_jobs,
    summarize_failure,
)
from tests.conftest import TestingSessionLocal
from tests.fakes import FakeMcPopen, mc_note

FAKE_LAUNCHER = ["/fake/python"]
FAKE_MC_ROOT = "/fake/MediaCrawler"

#: 评论补抓喂进 --specified_id 的真实形态：带查询串、含 == 与 &（不过 shell）。
XHS_URL_WITH_TOKEN = (
    "https://www.xiaohongshu.com/explore/abc?xsec_token=TOKEN&xsec_source=pc_search"
)

#: 任务输出目录必须指到 tmp（conftest._isolate_crawl_output），否则假产物
#: 会写进开发机真实的 materials/crawl/。
pytestmark = pytest.mark.usefixtures("_isolate_crawl_output")


@pytest.fixture(autouse=True)
def _reset_fake_mc_popen():
    """每个用例开始前清空 FakeMcPopen 实例记录。"""
    FakeMcPopen.reset()
    yield
    FakeMcPopen.reset()


def _make_job(db_session, **overrides) -> CrawlJob:
    """通过服务层创建一个真实任务（输出目录、expected_count 都走真实逻辑）。"""
    payload = CrawlJobCreate(
        platform="xhs",
        crawler_type="search",
        login_type="qrcode",
        keywords=["保温杯"],
        **overrides,
    )
    return CrawlJobService(db_session).create_job(payload)


def _make_runner(scripts=None, **overrides) -> CrawlRunner:
    """构造注入 FakeMcPopen 的执行器。

    Args:
        scripts: 逐次调用 popen 时使用的行为脚本列表；None 表示全部默认成功。
        overrides: 透传给 CrawlRunner 的其他参数（如 timeout_seconds），
            会盖掉默认值。
    """
    script_iter = iter(scripts) if scripts is not None else None

    def factory(*args, **kwargs):
        script = next(script_iter) if script_iter is not None else {}
        return FakeMcPopen(*args, script=script, **kwargs)

    # 默认 tick / progress 都设成 0：测试里子进程是假的，没什么可等；
    # launcher 直接注入假前缀，不触发真实探测。
    options = {
        "session_factory": TestingSessionLocal,
        "popen_factory": factory,
        "launcher": FAKE_LAUNCHER,
        "mc_root": FAKE_MC_ROOT,
        "tick_seconds": 0,
        "progress_seconds": 0,
    }
    options.update(overrides)
    return CrawlRunner(**options)


def _refresh(db_session, job: CrawlJob) -> CrawlJob:
    """丢弃会话缓存重新读库（runner 用的是另一条会话）。"""
    db_session.expire_all()
    return db_session.get(CrawlJob, job.id)


def _write_jsonl(output_dir: Path, platform: str, name: str, rows) -> Path:
    """按 MC 的落盘约定写一份 jsonl：{out}/{platform}/jsonl/{name}。"""
    target = output_dir / platform / "jsonl"
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


class TestBuildArgv:
    """命令行拼装：前缀、模式专属参数、bool 字符串化、白名单。"""

    def test_launcher_prefix_then_main_py(self, tmp_path):
        argv = build_argv(
            FAKE_LAUNCHER,
            platform="xhs",
            crawler_type="search",
            login_type="qrcode",
            params={"keywords": ["保温杯"]},
            output_dir=str(tmp_path),
        )
        assert argv[: len(FAKE_LAUNCHER)] == FAKE_LAUNCHER
        assert argv[len(FAKE_LAUNCHER)] == "main.py"
        assert argv[argv.index("--platform") + 1] == "xhs"
        assert argv[argv.index("--type") + 1] == "search"
        assert argv[argv.index("--lt") + 1] == "qrcode"

    def test_save_data_path_and_option(self, tmp_path):
        """必须落 jsonl 到任务专属目录 —— 结果读取（crawl_results）依赖这个约定。"""
        argv = build_argv(
            FAKE_LAUNCHER,
            platform="xhs",
            crawler_type="search",
            login_type="qrcode",
            params={},
            output_dir=str(tmp_path),
        )
        assert argv[argv.index("--save_data_option") + 1] == "jsonl"
        assert argv[argv.index("--save_data_path") + 1] == str(tmp_path)

    def test_search_keywords_joined(self, tmp_path):
        argv = build_argv(
            FAKE_LAUNCHER,
            platform="xhs",
            crawler_type="search",
            login_type="qrcode",
            params={"keywords": ["保温杯", "焖烧杯"]},
            output_dir=str(tmp_path),
        )
        assert argv[argv.index("--keywords") + 1] == "保温杯,焖烧杯"

    def test_detail_uses_specified_id(self, tmp_path):
        argv = build_argv(
            FAKE_LAUNCHER,
            platform="dy",
            crawler_type="detail",
            login_type="qrcode",
            params={"ids": ["https://v.douyin.com/x/", "712345"]},
            output_dir=str(tmp_path),
        )
        assert argv[argv.index("--specified_id") + 1] == "https://v.douyin.com/x/,712345"
        assert "--keywords" not in argv

    def test_creator_uses_creator_id(self, tmp_path):
        argv = build_argv(
            FAKE_LAUNCHER,
            platform="zhihu",
            crawler_type="creator",
            login_type="qrcode",
            params={"creators": ["https://www.zhihu.com/people/a"]},
            output_dir=str(tmp_path),
        )
        assert argv[argv.index("--creator_id") + 1] == "https://www.zhihu.com/people/a"

    def test_cookies_only_for_cookie_login(self, tmp_path):
        common = dict(
            platform="xhs",
            crawler_type="search",
            params={"keywords": ["a"]},
            output_dir=str(tmp_path),
        )
        argv = build_argv(FAKE_LAUNCHER, login_type="cookie", cookies="a=1;b=2", **common)
        assert argv[argv.index("--cookies") + 1] == "a=1;b=2"

        argv = build_argv(FAKE_LAUNCHER, login_type="qrcode", cookies="a=1", **common)
        assert "--cookies" not in argv  # 扫码登录不该把 cookie 串传下去

    def test_bool_params_as_strings(self, tmp_path):
        """MC 的 bool 参数声明为 str 再 str2bool，必须传 "true"/"false"。"""
        argv = build_argv(
            FAKE_LAUNCHER,
            platform="xhs",
            crawler_type="search",
            login_type="qrcode",
            params={
                "keywords": ["a"],
                "get_comments": True,
                "get_sub_comments": False,
                "headless": True,
            },
            output_dir=str(tmp_path),
        )
        assert argv[argv.index("--get_comment") + 1] == "true"
        assert argv[argv.index("--get_sub_comment") + 1] == "false"
        assert argv[argv.index("--headless") + 1] == "true"

    def test_non_whitelisted_params_ignored(self, tmp_path):
        """params 来自数据库 json 列，白名单以外的键一律不进 argv。"""
        argv = build_argv(
            FAKE_LAUNCHER,
            platform="xhs",
            crawler_type="search",
            login_type="qrcode",
            params={"keywords": ["a"], "evil": "--rm -rf"},
            output_dir=str(tmp_path),
        )
        assert "evil" not in argv
        assert "--rm" not in argv

    def test_comment_refetch_argv(self, tmp_path):
        """评论补抓派生任务的 argv：detail 模式 + 只喂一条链接 + 开着评论。

        链接里带 `?xsec_token=…&…` 这类字符，走 list argv 不经过 shell，
        原样作为一个参数传下去（与 detail 模式的既有形态一致）。
        """
        url = XHS_URL_WITH_TOKEN
        argv = build_argv(
            FAKE_LAUNCHER,
            platform="xhs",
            crawler_type="detail",
            login_type="qrcode",
            params={
                "ids": [url],
                "max_notes": 1,
                "get_comments": True,
                "get_sub_comments": True,
                "max_comments": 50,
            },
            output_dir=str(tmp_path),
        )
        assert argv[argv.index("--type") + 1] == "detail"
        assert argv[argv.index("--specified_id") + 1] == url
        assert argv[argv.index("--crawler_max_notes_count") + 1] == "1"
        assert argv[argv.index("--get_comment") + 1] == "true"
        assert argv[argv.index("--get_sub_comment") + 1] == "true"
        assert argv[argv.index("--max_comments_count_singlenotes") + 1] == "50"


class TestHelpers:
    """纯函数小工具。"""

    def test_summarize_failure_prefers_error_lines(self):
        tail = "INFO 开始搜索\nINFO 翻页中\nERROR 搜索失败：风控拦截\nINFO 收尾"
        assert summarize_failure(tail) == "ERROR 搜索失败：风控拦截"

    def test_summarize_failure_falls_back_to_last_line(self):
        assert summarize_failure("普通日志\n最后一行") == "最后一行"

    def test_summarize_failure_empty(self):
        assert summarize_failure("") == ""

    def test_estimate_expected_count(self):
        assert estimate_expected_count("detail", {"ids": ["a", "b"]}) == 2
        assert estimate_expected_count("search", {"keywords": ["a", "b"], "max_notes": 20}) == 40
        assert estimate_expected_count("creator", {"creators": ["a"], "max_notes": 10}) == 10
        # 模式参数缺失时按 1 个计（创建任务时已校验必填，这里是兜底口径）
        assert estimate_expected_count("search", {"max_notes": 20}) == 20

    def test_is_our_child_rejects_nonpositive_pid(self):
        assert is_our_child(0) is False
        assert is_our_child(-1) is False

    # ------------------------------------------------------------------
    # detect_phase：从 MC 日志尾部推断当前阶段
    # ------------------------------------------------------------------

    def test_detect_phase_empty_log_is_starting(self):
        assert detect_phase("", login_type="qrcode", crawled_count=0) == "starting"

    def test_detect_phase_whitespace_log_is_starting(self):
        assert detect_phase("  \n  ", login_type="cookie", crawled_count=0) == "starting"

    def test_detect_phase_cookie_login(self):
        tail = "INFO [XiaoHongShuLogin.login_by_cookies] Begin login xiaohongshu by cookie ..."
        assert detect_phase(tail, login_type="cookie", crawled_count=0) == "login_cookie"

    def test_detect_phase_qrcode_waiting_scan(self):
        tail = (
            "INFO [XiaoHongShuLogin.login_by_qrcode] Begin login xiaohongshu by qrcode\n"
            "INFO waiting for scan code login, remaining time is 120s"
        )
        assert detect_phase(tail, login_type="qrcode", crawled_count=0) == "login_scan"

    def test_detect_phase_login_redirect(self):
        tail = (
            "INFO [XiaoHongShuLogin.login_by_qrcode] Begin login xiaohongshu by qrcode\n"
            "INFO waiting for scan code login, remaining time is 120s\n"
            "INFO Login successful then wait for 3 seconds redirect"
        )
        assert detect_phase(tail, login_type="qrcode", crawled_count=0) == "login_redirect"

    def test_detect_phase_crawling_by_search_marker(self):
        tail = "INFO [XiaoHongShuCrawler.search] Begin search Xiaohongshu keywords"
        assert detect_phase(tail, login_type="qrcode", crawled_count=0) == "crawling"

    def test_detect_phase_crawling_by_note_detail(self):
        tail = "INFO [get_note_detail_async_task] Begin get note detail, note_id: abc123"
        assert detect_phase(tail, login_type="qrcode", crawled_count=0) == "crawling"

    def test_detect_phase_crawling_by_crawled_count(self):
        """即使日志标记被冲掉，只要已有产物就是 crawling。"""
        tail = "INFO some random line\nINFO another line"
        assert detect_phase(tail, login_type="qrcode", crawled_count=5) == "crawling"

    def test_detect_phase_finishing_overrides_all(self):
        tail = (
            "INFO Begin search Xiaohongshu keywords\n"
            "INFO [BrowserLauncher] Closing browser process"
        )
        assert detect_phase(tail, login_type="qrcode", crawled_count=10) == "finishing"

    def test_detect_phase_last_marker_wins(self):
        """日志里同一阶段可能反复出现（重试），取最后命中的。"""
        tail = (
            "INFO waiting for scan code login, remaining time is 120s\n"
            "INFO waiting for scan code login, remaining time is 60s\n"
            "INFO Login successful then wait for 3 seconds redirect"
        )
        assert detect_phase(tail, login_type="qrcode", crawled_count=0) == "login_redirect"

    def test_detect_phase_cookie_flow_full(self):
        """cookie 登录完整流：by cookie → crawling。"""
        cookie_tail = "INFO Begin login xiaohongshu by cookie ..."
        assert detect_phase(cookie_tail, login_type="cookie", crawled_count=0) == "login_cookie"
        crawl_tail = cookie_tail + "\nINFO Begin search Xiaohongshu keywords"
        assert detect_phase(crawl_tail, login_type="cookie", crawled_count=0) == "crawling"


class TestRunJobSuccess:
    """顺利跑完一条任务的完整链路。"""

    def test_success_with_notes(self, db_session):
        job = _make_job(db_session)
        runner = _make_runner()
        assert runner.run_job(job.id) is True

        fresh = _refresh(db_session, job)
        assert fresh.status == CrawlJobStatus.SUCCESS
        assert fresh.note_count == 3
        assert fresh.error_message == ""
        assert fresh.started_at is not None
        assert fresh.finished_at is not None
        assert fresh.child_pid is None  # 收尾必须清掉子进程句柄

    def test_subprocess_receives_job_context(self, db_session):
        """argv / cwd / stdio 的关键约定：save_data_path 指向任务目录，cwd 是 MC 根。"""
        job = _make_job(db_session)
        _make_runner().run_job(job.id)

        assert len(FakeMcPopen.instances) == 1
        process = FakeMcPopen.instances[0]
        assert process.cwd == FAKE_MC_ROOT
        assert process.argv[process.argv.index("--save_data_path") + 1] == job.output_dir
        assert process.start_new_session is True  # 整组 kill 的前提
        # Windows 侧的等价隔离：不吃后端控制台的 Ctrl+C（start_new_session 在
        # Windows 上是空操作，靠 CREATE_NEW_PROCESS_GROUP 补）
        assert process.creationflags == media_tools.CHILD_CREATION_FLAGS

    def test_progress_updates_while_running(self, db_session):
        """进度 = 已落盘 jsonl 行数，轮询期间就该刷新（不是只在收尾时）。"""
        job = _make_job(db_session)
        seen = []

        def probe(proc, count):
            if count == 2:  # 第 2 次 poll 时：第 1 条已写、进度已落库
                with TestingSessionLocal() as db:
                    seen.append(db.get(CrawlJob, job.id).crawled_count)

        runner = _make_runner(
            scripts=[{"notes_per_poll": 1, "polls_before_exit": 2, "on_poll": probe}]
        )
        runner.run_job(job.id)

        assert seen == [1]
        fresh = _refresh(db_session, job)
        assert fresh.status == CrawlJobStatus.SUCCESS
        assert fresh.note_count == 3
        assert fresh.crawled_count == 3


class TestResultJudgement:
    """结果判定以产物为准（见模块头注释）。"""

    def test_nonzero_exit_with_notes_is_success(self, db_session):
        """MC 收尾清理阶段常报错但数据已落盘：这种情况不该判失败。"""
        job = _make_job(db_session)
        runner = _make_runner(scripts=[{"exit_code": 5}])
        runner.run_job(job.id)

        fresh = _refresh(db_session, job)
        assert fresh.status == CrawlJobStatus.SUCCESS
        assert fresh.note_count == 3
        assert "5" in fresh.error_message  # 退出码写进备注，不藏

    def test_zero_exit_without_notes_is_failed(self, db_session):
        """风控拦截时 MC 可能「正常」退出但一条没抓到 —— 以产物为准判失败。"""
        job = _make_job(db_session)
        runner = _make_runner(
            scripts=[{"no_output": True, "log_lines": ["ERROR 搜索失败：风控拦截"]}]
        )
        runner.run_job(job.id)

        fresh = _refresh(db_session, job)
        assert fresh.status == CrawlJobStatus.FAILED
        assert "风控" in fresh.error_message

    def test_log_written_to_job_dir(self, db_session, tmp_path):
        """子进程 stdout 必须落到任务目录的 mc.log（失败摘要的来源）。"""
        job = _make_job(db_session)
        runner = _make_runner(scripts=[{"no_output": True, "log_lines": ["line-1"]}])
        runner.run_job(job.id)

        assert Path(job.log_path).read_text(encoding="utf-8").strip() == "line-1"


class TestCancel:
    """取消：杀进程组、状态转 cancelled、已抓到的保留。"""

    def test_cancel_mid_run_keeps_partial_results(self, db_session):
        job = _make_job(db_session)

        def cancel_on_first_poll(proc, count):
            if count == 1:
                with TestingSessionLocal() as db:
                    CrawlJobService(db).cancel_job(job.id)

        runner = _make_runner(
            scripts=[
                {
                    "hang": True,
                    "notes_per_poll": 2,  # 第 1 次 poll 就把仅有的两条笔记全部落盘
                    "notes": [mc_note("n1"), mc_note("n2")],
                    "on_poll": cancel_on_first_poll,
                }
            ],
            stop_grace_seconds=0,
        )
        assert runner.run_job(job.id) is True

        fresh = _refresh(db_session, job)
        assert fresh.status == CrawlJobStatus.CANCELLED
        assert fresh.note_count == 2  # 取消≠清零
        assert fresh.child_pid is None


class TestTimeout:
    """整体硬超时。"""

    def test_timeout_fails_and_keeps_notes(self, db_session):
        job = _make_job(db_session)
        runner = _make_runner(
            scripts=[{"hang": True, "notes_per_poll": 3}],
            timeout_seconds=0,
            stop_grace_seconds=0,
        )
        runner.run_job(job.id)

        fresh = _refresh(db_session, job)
        assert fresh.status == CrawlJobStatus.FAILED
        assert "超时" in fresh.error_message
        assert fresh.note_count == 3  # 超时前抓到的不丢


class TestLauncherMissing:
    """排队期间 MC 被挪走：任务必须落到 failed 终态（不许永远 running）。"""

    def test_missing_launcher_fails_job(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "app.services.crawl_runner.detect",
            lambda: McInstall(
                installed=False,
                launcher=(),
                kind="",
                mc_root="",
                python_version="",
                detail="没找到 MediaCrawler",
            ),
        )
        job = _make_job(db_session)
        runner = _make_runner(launcher=None)
        assert runner.run_job(job.id) is True

        fresh = _refresh(db_session, job)
        assert fresh.status == CrawlJobStatus.FAILED
        assert "MediaCrawler" in fresh.error_message
        assert FakeMcPopen.instances == []  # 没起子进程


class TestClaimAndRecover:
    """DB 即队列：认领顺序、取消抢锁、启动回收。"""

    def test_claim_picks_earliest_pending(self, db_session):
        job1 = _make_job(db_session)
        job2 = _make_job(db_session)
        assert claim_next_pending_id(TestingSessionLocal) == job1.id
        assert job1.id < job2.id

    def test_run_job_skips_cancelled(self, db_session):
        """已取消的任务认领失败：不起子进程、返回 False。"""
        job = _make_job(db_session)
        CrawlJobService(db_session).cancel_job(job.id)

        assert _make_runner().run_job(job.id) is False
        assert FakeMcPopen.instances == []
        assert _refresh(db_session, job).status == CrawlJobStatus.CANCELLED

    def test_recover_marks_running_failed_with_notes(self, db_session, tmp_path):
        """服务重启回收 running 任务：状态 failed，已抓到的计入 note_count。"""
        job = _make_job(db_session)
        out = tmp_path / "job_out"
        _write_jsonl(out, "xhs", "search_contents_a.jsonl", [mc_note("n1"), mc_note("n2")])

        job.status = CrawlJobStatus.RUNNING
        job.output_dir = str(out)
        job.log_path = str(out / "mc.log")
        db_session.commit()

        assert recover_interrupted_jobs(TestingSessionLocal) == 1
        fresh = _refresh(db_session, job)
        assert fresh.status == CrawlJobStatus.FAILED
        assert "中断" in fresh.error_message
        assert fresh.note_count == 2
        assert fresh.child_pid is None

    def test_recover_ignores_pending(self, db_session):
        """pending 不动 —— 服务重启后 DB 队列会自动接手。"""
        _make_job(db_session)
        assert recover_interrupted_jobs(TestingSessionLocal) == 0


class TestWorker:
    """工作线程外壳：注入 claim_next / runner，完全不碰 DB 与真子进程。"""

    def test_worker_drains_queue_and_stops(self):
        ran = []

        class FakeRunner:
            def run_job(self, job_id, stop_event=None):
                ran.append(job_id)
                return True

        queue = iter([101, 102])
        worker = CrawlJobWorker(
            claim_next=lambda: next(queue, None),
            runner_factory=lambda: FakeRunner(),
            poll_seconds=0.01,
        )
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while len(ran) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            worker.stop(grace=1)

        assert ran == [101, 102]
        assert worker._thread is None  # stop 后线程句柄清空，start 可再次调用

    def test_worker_survives_runner_exception(self):
        """执行器抛异常不能让线程静默死亡（否则后续任务永远 pending）。"""
        ran = []

        class BoomRunner:
            calls = 0

            def run_job(self, job_id, stop_event=None):
                BoomRunner.calls += 1
                if BoomRunner.calls == 1:
                    raise RuntimeError("boom")
                ran.append(job_id)
                return True

        worker = CrawlJobWorker(
            claim_next=lambda: 1,
            runner_factory=BoomRunner,
            poll_seconds=0.01,
        )
        worker.start()
        try:
            deadline = time.monotonic() + 2
            while not ran and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            worker.stop(grace=1)

        assert ran and ran[0] == 1  # 第一次炸了，第二次照常执行
