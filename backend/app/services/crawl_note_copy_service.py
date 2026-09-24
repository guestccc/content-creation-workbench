"""素材抓取笔记 AI 文案的数据库服务层。

职责：按（任务, 笔记）读/写生成结果。与 crawl_note_copy.py（纯逻辑）分开：
那边不碰 DB、不碰 HTTP，这边负责「找到那条笔记 → 调 AI → 落库」。

存储语义：一条（crawl_job_id, note_id）只留最新一份，「换一批」是覆盖
（唯一约束 uq_crawl_note_ai_copy 兜底，并发双击撞约束时转成更新）。

生成走**流式**（用户在弹窗里要看 AI 的思考过程）：路由把 stream() 产出的
事件直接转成 SSE 帧推给前端，一次 AI 调用 5-30 秒，sync 生成器由 Starlette
丢线程池迭代，不阻塞事件循环；一次只生成一条笔记，不需要后台任务表。

生成器里为什么**不能**用请求作用域的会话：FastAPI 对 yield 依赖的退出时机
与 StreamingResponse 的消费顺序不保证（Starlette 是响应阶段才迭代 body），
流还没跑完会话可能已经被关掉。所以改成显式传入 session_factory —— 直接
`SessionLocal()` 也不行，那会绕过测试对 get_db 的覆盖，把测试数据写进真实库。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.db.session import transaction
from app.models.crawl_ai_copy import CrawlNoteAiCopy
from app.models.crawl_job import CrawlJob
from app.services import crawl_note_copy, crawl_results
from app.services.ai_client import AiError, stream_chat

logger = get_logger(__name__)


@dataclass(frozen=True)
class CopyContext:
    """流式生成所需的全部输入，由 prepare() 在请求作用域内备好。

    prepare 之后生成器只读这个对象，不再碰请求会话 —— 见模块 docstring。
    """

    job_id: int
    note_id: str
    note: dict
    platform: str


class CrawlNoteCopyService:
    """素材抓取笔记的 AI 文案服务。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    def _get_job_or_404(self, job_id: int) -> CrawlJob:
        """按 ID 取抓取任务，不存在则抛 NotFoundError。"""
        job = self.db.get(CrawlJob, job_id)
        if job is None:
            raise NotFoundError(f"素材抓取任务不存在：id={job_id}")
        return job

    def get_copy(self, job_id: int, note_id: str) -> Optional[CrawlNoteAiCopy]:
        """读取一条笔记已落库的 AI 文案；没生成过返回 None（「还没生成」是正常态）。

        Raises:
            NotFoundError: 抓取任务不存在。
            DatabaseError: 读库失败。
        """
        self._get_job_or_404(job_id)
        try:
            stmt = select(CrawlNoteAiCopy).where(
                CrawlNoteAiCopy.crawl_job_id == job_id,
                CrawlNoteAiCopy.note_id == note_id,
            )
            return self.db.execute(stmt).scalar_one_or_none()
        except SQLAlchemyError as exc:
            logger.exception("查询笔记 AI 文案失败 | job=%s | note=%s", job_id, note_id)
            raise DatabaseError("查询笔记 AI 文案失败") from exc

    def prepare(self, job_id: int, note_id: str) -> CopyContext:
        """请求作用域内跑的预检：任务在不在、笔记在不在。

        单拎一趟的理由：HTTP 状态码在开流之后再也改不了，「任务不存在」
        「笔记不在结果里」这类能正经回 404 的情况必须赶在返回
        StreamingResponse 之前判掉（见模块 docstring）。

        Raises:
            NotFoundError: 任务不存在，或这条笔记不在任务结果里。
        """
        job = self._get_job_or_404(job_id)
        return CopyContext(
            job_id=job_id,
            note_id=note_id,
            note=self._find_note(job, note_id),
            platform=job.platform,
        )

    def stream(
        self,
        ctx: CopyContext,
        session_factory: Callable[[], Session],
        *,
        chat_fn: Optional[Callable] = None,
    ) -> Iterator[Tuple[str, dict]]:
        """流式生成（或换一批）一条笔记的 AI 文案，产出 (事件名, 数据) 二元组。

        事件三种：

        - `("reasoning", {"text": 增量})` —— 思维链增量，空增量不产出；
        - `("done", {"row": CrawlNoteAiCopy})` —— 解析 + 落库完成，路由再转成响应模型；
        - `("error", {"kind": ..., "message": ...})` —— 失败。kind 是 `AiError.kind`
          外加一个 `"db"`（写库失败），路由层映射成对外的错误码。

        **异常一律就地转成 error 事件，不往上抛**：流已经开出去了，这时候抛异常
        既改不了状态码，前端也只会看到一条断掉的长连接，什么提示都没有。

        Args:
            ctx: prepare() 的产物，生成器只读它 —— 见模块 docstring。
            session_factory: 无参构造 Session 的工厂（路由注入，测试覆盖成内存库）。
                生成器跑在响应阶段，用不了请求作用域的会话。
            chat_fn: AI 调用点（默认模块里的 stream_chat，**调用时**才取——
                默认参数在函数定义时就绑死了，monkeypatch 模块属性会不生效）。
                测试传假生成器，不必 MockTransport 绕一层（同 finalcut_copy_runner
                的注入口径）。
        """
        job_id, note_id = ctx.job_id, ctx.note_id
        chat = chat_fn or stream_chat

        reasoning_parts: List[str] = []
        content_parts: List[str] = []
        usage: dict = {}

        # 思维链边到边推：用户看的就是它逐字长出来的过程，攒完再发就没意义了
        try:
            for delta in chat(
                crawl_note_copy.build_copy_messages(ctx.note, ctx.platform)
            ):
                if delta.reasoning:
                    reasoning_parts.append(delta.reasoning)
                    yield "reasoning", {"text": delta.reasoning}
                if delta.content:
                    content_parts.append(delta.content)
                if delta.usage:
                    usage = delta.usage
        except AiError as exc:
            logger.warning(
                "笔记 AI 文案生成失败 | job=%s | note=%s | kind=%s",
                job_id, note_id, exc.kind,
            )
            yield "error", {"kind": exc.kind, "message": exc.user_message}
            return
        except Exception as exc:
            # chat_fn 是注入口，什么都可能抛（含 AI 调用之外的意外）；
            # 不用 except Exception 兜住就会变成「流莫名其妙断掉」
            logger.exception("笔记 AI 文案生成异常 | job=%s | note=%s", job_id, note_id)
            yield "error", {"kind": "unknown", "message": f"AI 调用失败：{exc}"}
            return

        content = "".join(content_parts)
        try:
            payload = crawl_note_copy.parse_copy_payload(content)
        except (AiError, ValueError) as exc:
            # 空内容 / 坏 JSON / 一条标题都没解析出来：都算「这轮返回不可用」。
            # 不落行：库里有内容 = 有一份能用的文案，「上次失败了」当场说即可。
            # AiError 的 __str__ 就是 user_message，两种异常统一取 str()
            logger.warning("笔记 AI 文案解析失败 | job=%s | note=%s | %s", job_id, note_id, exc)
            yield "error", {"kind": "bad_response", "message": str(exc)}
            return

        model = self._settings_model()
        tokens = self._tokens_used(usage)
        # 独立会话，不用 self.db —— 见 stream() 的 Args 说明。
        # 会话在 yield 之前就关掉：yield 在 except/finally 里面的话，关闭要等
        # 生成器下次被恢复（或客户端断开触发 close()）才发生，多绕一层
        db_error = ""
        db = session_factory()
        try:
            row = self._persist(
                db,
                job_id=job_id,
                note_id=note_id,
                payload=payload,
                reasoning="".join(reasoning_parts),
                model=model,
                tokens=tokens,
                raw=content,
            )
        except DatabaseError as exc:
            row = None
            db_error = str(exc)
        finally:
            db.close()

        if row is None:
            # 写库失败：事务已在 _persist 里回滚过，就地转成 error 帧
            logger.warning("笔记 AI 文案写库失败 | job=%s | note=%s | %s", job_id, note_id, db_error)
            yield "error", {"kind": "db", "message": db_error}
            return

        logger.info(
            "笔记 AI 文案已生成 | job=%s | note=%s | tokens=%s",
            job_id, note_id, tokens,
        )
        yield "done", {"row": row}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _find_note(self, job: CrawlJob, note_id: str) -> dict:
        """从任务产物里找到那条笔记（生成文案要拿它的标题/正文喂给 AI）。"""
        notes = crawl_results.collect_results(Path(job.output_dir), job.platform)
        note = crawl_results.find_note(notes, note_id)
        if note is None:
            raise NotFoundError(
                "这条笔记不在任务结果里（结果可能已变化，请刷新结果列表）"
            )
        return note

    def _persist(
        self,
        db: Session,
        *,
        job_id: int,
        note_id: str,
        payload: dict,
        reasoning: str,
        model: str,
        tokens: int,
        raw: str,
    ) -> CrawlNoteAiCopy:
        """把一份生成结果写进库（一条（任务, 笔记）只留最新一份）。

        事务里只有写 —— AI 调用早已在事务外做完，不给长请求占着锁。

        Raises:
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(db):
                row = self._find_row(db, job_id, note_id)
                if row is None:
                    row = CrawlNoteAiCopy(crawl_job_id=job_id, note_id=note_id)
                    db.add(row)
                self._fill_row(row, payload, reasoning, model, tokens, raw)
                db.flush()
        except IntegrityError:
            # 并发双击撞唯一约束：另一个请求已经插了行，转成覆盖它
            logger.info("AI 文案并发生成撞唯一约束，转为更新 | job=%s | note=%s", job_id, note_id)
            try:
                with transaction(db):
                    row = self._find_row(db, job_id, note_id)
                    if row is None:  # 理论上不会发生（对方已提交），兜底防 None
                        raise DatabaseError("保存笔记 AI 文案失败")
                    self._fill_row(row, payload, reasoning, model, tokens, raw)
                    db.flush()
            except SQLAlchemyError as exc:
                logger.exception("保存笔记 AI 文案失败 | job=%s | note=%s", job_id, note_id)
                raise DatabaseError("保存笔记 AI 文案失败") from exc
        except SQLAlchemyError as exc:
            logger.exception("保存笔记 AI 文案失败 | job=%s | note=%s", job_id, note_id)
            raise DatabaseError("保存笔记 AI 文案失败") from exc
        return row

    @staticmethod
    def _fill_row(
        row: CrawlNoteAiCopy,
        payload: dict,
        reasoning: str,
        model: str,
        tokens: int,
        raw: str,
    ) -> None:
        """把这一轮的生成结果盖到行上（插入与并发转更新两条路径共用）。"""
        row.payload = payload
        row.reasoning = reasoning
        row.model = model
        row.tokens_used = tokens
        row.raw_response = raw

    def _find_row(self, db: Session, job_id: int, note_id: str) -> Optional[CrawlNoteAiCopy]:
        stmt = select(CrawlNoteAiCopy).where(
            CrawlNoteAiCopy.crawl_job_id == job_id,
            CrawlNoteAiCopy.note_id == note_id,
        )
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def _settings_model() -> str:
        """当前生效的模型名（快照进行，事后改配置不影响已生成的记录）。"""
        return settings.AI_MODEL

    @staticmethod
    def _tokens_used(usage: dict) -> int:
        """从 chat/completions 的 usage 里取 total_tokens，拿不到按 0。"""
        try:
            return int(usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            return 0


def delete_copies_for_jobs(db: Session, job_ids: List[int]) -> None:
    """删掉一批抓取任务下的全部 AI 文案。

    供 crawl_job_service 删除任务时在**同一事务**里调用（调用点必须在
    with transaction(db): 块内、在 db.delete(job) 之前）—— 任务删不掉时
    文案行也不会被误删。表上没建外键（见模型 docstring），级联只能靠这里。
    """
    if not job_ids:
        return
    db.execute(
        delete(CrawlNoteAiCopy).where(CrawlNoteAiCopy.crawl_job_id.in_(job_ids))
    )
