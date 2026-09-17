"""发布任务业务逻辑层。

任务生命周期：

    pending ──认领──> running ──成功──> success
       │                 │
       │                 └──失败──> 未超重试上限 → 回到 pending
       │                           超过上限     → failed
       │
       └──取消──> cancelled        failed / cancelled ──重试──> pending

终态（success / failed / cancelled）不允许再发生自动流转，
只能通过显式的 retry 接口重置回队列。
"""

from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.db.session import transaction
from app.models.account import Account
from app.models.content import Content, utcnow
from app.models.publish_task import PublishTask, PublishTaskStatus
from app.schemas.publish_task import (
    PublishTaskBatchCreate,
    PublishTaskClaim,
    PublishTaskCreate,
    PublishTaskReport,
)

logger = get_logger(__name__)


class PublishTaskService:
    """发布任务服务。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    # ------------------------------------------------------------------
    # 内部校验
    # ------------------------------------------------------------------

    def _ensure_content_exists(self, content_id: int) -> Content:
        """校验内容存在。"""
        content = self.db.get(Content, content_id)
        if content is None:
            raise NotFoundError(f"内容不存在：id={content_id}")
        return content

    def _ensure_account_exists(self, account_id: int) -> Account:
        """校验账号存在。"""
        account = self.db.get(Account, account_id)
        if account is None:
            raise NotFoundError(f"账号不存在：id={account_id}")
        return account

    def _get_task_or_404(self, task_id: int) -> PublishTask:
        """按 ID 取任务，不存在则抛 NotFoundError。"""
        task = self.db.get(PublishTask, task_id)
        if task is None:
            raise NotFoundError(f"发布任务不存在：id={task_id}")
        return task

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_tasks(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None,
        account_id: Optional[int] = None,
        content_id: Optional[int] = None,
    ) -> Tuple[List[PublishTask], int]:
        """分页查询发布任务。

        Returns:
            (当前页数据, 满足条件的总条数)
        """
        try:
            conditions = []
            if status:
                conditions.append(PublishTask.status == status)
            if account_id:
                conditions.append(PublishTask.account_id == account_id)
            if content_id:
                conditions.append(PublishTask.content_id == content_id)

            count_stmt = select(func.count()).select_from(PublishTask)
            list_stmt = select(PublishTask)
            for condition in conditions:
                count_stmt = count_stmt.where(condition)
                list_stmt = list_stmt.where(condition)

            total = self.db.execute(count_stmt).scalar_one()

            list_stmt = (
                list_stmt.order_by(PublishTask.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            items = list(self.db.execute(list_stmt).scalars().all())
            return items, int(total)
        except SQLAlchemyError as exc:
            logger.exception("查询发布任务列表失败")
            raise DatabaseError("查询发布任务列表失败") from exc

    def get_task(self, task_id: int) -> PublishTask:
        """按 ID 获取发布任务。"""
        try:
            return self._get_task_or_404(task_id)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("查询发布任务失败 | id=%s", task_id)
            raise DatabaseError("查询发布任务失败") from exc

    def get_statistics(self) -> Dict:
        """统计各状态的任务数量，供客户端与工作台展示。"""
        try:
            stmt = select(PublishTask.status, func.count(PublishTask.id)).group_by(
                PublishTask.status
            )
            rows = self.db.execute(stmt).all()

            by_status: Dict[str, int] = {status: 0 for status in PublishTaskStatus.ALL}
            for status, count in rows:
                by_status[status] = int(count)

            return {"total": sum(by_status.values()), "by_status": by_status}
        except SQLAlchemyError as exc:
            logger.exception("统计发布任务失败")
            raise DatabaseError("统计发布任务失败") from exc

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    def create_task(self, payload: PublishTaskCreate) -> PublishTask:
        """创建单个发布任务。"""
        try:
            with transaction(self.db):
                self._ensure_content_exists(payload.content_id)
                self._ensure_account_exists(payload.account_id)

                task = PublishTask(
                    content_id=payload.content_id,
                    account_id=payload.account_id,
                    status=PublishTaskStatus.PENDING,
                    scheduled_at=payload.scheduled_at,
                    max_retries=payload.max_retries,
                )
                self.db.add(task)
                self.db.flush()

            logger.info(
                "发布任务创建成功 | id=%s | content=%s | account=%s",
                task.id,
                task.content_id,
                task.account_id,
            )
            return task
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("发布任务创建失败 | content=%s", payload.content_id)
            raise DatabaseError("发布任务创建失败") from exc

    def create_batch(self, payload: PublishTaskBatchCreate) -> List[PublishTask]:
        """批量创建发布任务：把同一条内容分发到多个账号。

        整批任务在同一事务中创建，任何一个账号不存在则整批回滚，
        避免出现「创建了一半」的中间状态。

        Raises:
            NotFoundError: 内容或某个账号不存在。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                self._ensure_content_exists(payload.content_id)

                # 一次性查出所有账号，避免逐个查询
                accounts = list(
                    self.db.execute(
                        select(Account).where(Account.id.in_(payload.account_ids))
                    )
                    .scalars()
                    .all()
                )
                found_ids = {account.id for account in accounts}
                missing = [aid for aid in payload.account_ids if aid not in found_ids]
                if missing:
                    raise NotFoundError(f"以下账号不存在：{missing}")

                tasks = [
                    PublishTask(
                        content_id=payload.content_id,
                        account_id=account_id,
                        status=PublishTaskStatus.PENDING,
                        scheduled_at=payload.scheduled_at,
                        max_retries=payload.max_retries,
                    )
                    for account_id in payload.account_ids
                ]
                self.db.add_all(tasks)
                self.db.flush()

            logger.info(
                "批量创建发布任务成功 | content=%s | 账号数=%s",
                payload.content_id,
                len(tasks),
            )
            return tasks
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("批量创建发布任务失败 | content=%s", payload.content_id)
            raise DatabaseError("批量创建发布任务失败") from exc

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------

    def claim_tasks(self, payload: PublishTaskClaim) -> List[PublishTask]:
        """客户端认领待执行任务，并把状态置为发布中。

        并发安全：整个「查询 + 更新」在同一事务内完成，并使用
        SELECT ... FOR UPDATE 行锁（PostgreSQL 下生效；SQLite 会忽略该子句，
        但其写事务本身是串行的），确保同一条任务不会被多个客户端重复认领。

        Returns:
            本次认领到的任务列表（可能为空）。
        """
        now = utcnow()
        try:
            with transaction(self.db):
                stmt = select(PublishTask).where(
                    PublishTask.status == PublishTaskStatus.PENDING,
                    # scheduled_at 为空表示尽快执行，也应被认领
                    (PublishTask.scheduled_at.is_(None))
                    | (PublishTask.scheduled_at <= now),
                )
                if payload.account_id is not None:
                    stmt = stmt.where(PublishTask.account_id == payload.account_id)

                stmt = (
                    stmt.order_by(
                        # 立即执行的任务（scheduled_at 为空）排在最前
                        PublishTask.scheduled_at.is_(None).desc(),
                        PublishTask.scheduled_at.asc(),
                        PublishTask.id.asc(),
                    )
                    .limit(payload.limit)
                    .with_for_update()
                )

                tasks = list(self.db.execute(stmt).scalars().all())

                for task in tasks:
                    task.status = PublishTaskStatus.RUNNING
                    task.started_at = now

                self.db.flush()

            logger.info("客户端认领任务 | 数量=%s | ids=%s", len(tasks), [t.id for t in tasks])
            return tasks
        except SQLAlchemyError as exc:
            logger.exception("认领发布任务失败")
            raise DatabaseError("认领发布任务失败") from exc

    def report_task(self, task_id: int, payload: PublishTaskReport) -> PublishTask:
        """客户端上报发布结果。

        失败时若未超过重试上限，任务自动回到队列等待下次认领；
        超过上限则置为 failed 终态。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务当前不处于发布中状态。
            DatabaseError: 写库失败（事务已回滚）。
        """
        now = utcnow()
        try:
            with transaction(self.db):
                task = self._get_task_or_404(task_id)

                if task.status != PublishTaskStatus.RUNNING:
                    raise ConflictError(
                        f"任务当前状态为 {task.status}，只有发布中(running)的任务可以上报结果"
                    )

                if payload.success:
                    task.status = PublishTaskStatus.SUCCESS
                    task.result_url = payload.result_url
                    task.error_message = ""
                    task.finished_at = now
                else:
                    task.error_message = payload.error_message or "发布失败，未提供具体原因"

                    if task.retry_count < task.max_retries:
                        # 还有重试机会：回到队列，清空本次执行痕迹
                        task.retry_count += 1
                        task.status = PublishTaskStatus.PENDING
                        task.started_at = None
                        task.finished_at = None
                        logger.warning(
                            "任务发布失败，进入重试 | id=%s | 第 %s 次重试",
                            task_id,
                            task.retry_count,
                        )
                    else:
                        task.status = PublishTaskStatus.FAILED
                        task.finished_at = now
                        logger.error(
                            "任务发布失败且已达重试上限 | id=%s | 原因=%s",
                            task_id,
                            task.error_message,
                        )

                self.db.flush()

            return task
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("上报发布结果失败 | id=%s", task_id)
            raise DatabaseError("上报发布结果失败") from exc

    def cancel_task(self, task_id: int) -> PublishTask:
        """取消任务。

        允许取消发布中(running)的任务；此时客户端仍可能上报结果，
        上报会因状态不匹配被拒绝（409），由客户端忽略即可。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务已处于终态。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                task = self._get_task_or_404(task_id)

                if task.status in PublishTaskStatus.TERMINAL:
                    raise ConflictError(f"任务已是终态（{task.status}），无法取消")

                task.status = PublishTaskStatus.CANCELLED
                task.finished_at = utcnow()
                self.db.flush()

            logger.info("发布任务已取消 | id=%s", task_id)
            return task
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("取消发布任务失败 | id=%s", task_id)
            raise DatabaseError("取消发布任务失败") from exc

    def retry_task(self, task_id: int) -> PublishTask:
        """把失败或已取消的任务重新放回队列，并重置重试计数。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务状态不允许重试。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                task = self._get_task_or_404(task_id)

                allowed = (PublishTaskStatus.FAILED, PublishTaskStatus.CANCELLED)
                if task.status not in allowed:
                    raise ConflictError(
                        f"只有失败或已取消的任务可以重试，当前状态：{task.status}"
                    )

                task.status = PublishTaskStatus.PENDING
                task.retry_count = 0
                task.error_message = ""
                task.result_url = ""
                task.started_at = None
                task.finished_at = None
                self.db.flush()

            logger.info("发布任务已重新入队 | id=%s", task_id)
            return task
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("重试发布任务失败 | id=%s", task_id)
            raise DatabaseError("重试发布任务失败") from exc

    def delete_task(self, task_id: int) -> None:
        """删除任务记录。

        仅允许删除终态任务，避免误删正在排队或执行中的任务。

        Raises:
            NotFoundError: 任务不存在。
            ConflictError: 任务尚未结束。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                task = self._get_task_or_404(task_id)

                if task.status not in PublishTaskStatus.TERMINAL:
                    raise ConflictError(
                        f"任务尚未结束（{task.status}），请先取消后再删除"
                    )

                self.db.delete(task)

            logger.info("发布任务已删除 | id=%s", task_id)
        except (NotFoundError, ConflictError):
            raise
        except SQLAlchemyError as exc:
            logger.exception("删除发布任务失败 | id=%s", task_id)
            raise DatabaseError("删除发布任务失败") from exc
