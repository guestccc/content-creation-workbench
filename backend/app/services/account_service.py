"""账号业务逻辑层。

所有写操作包裹在事务中，失败整体回滚。
凭证不在此层处理——平台登录凭证由客户端本地加密保存。
"""

from typing import List, Optional, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.db.session import transaction
from app.models.account import Account
from app.models.publish_task import PublishTask, PublishTaskStatus
from app.schemas.account import AccountCreate, AccountUpdate

logger = get_logger(__name__)


class AccountService:
    """平台账号服务。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_accounts(
        self,
        *,
        platform: Optional[str] = None,
        status: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> Tuple[List[Account], int]:
        """查询账号列表（账号数量通常有限，不做分页）。

        Args:
            platform: 按平台过滤。
            status: 按状态过滤。
            keyword: 昵称或备注模糊匹配。

        Returns:
            (账号列表, 总条数)
        """
        try:
            conditions = []
            if platform:
                conditions.append(Account.platform == platform)
            if status:
                conditions.append(Account.status == status)
            if keyword:
                pattern = f"%{keyword}%"
                conditions.append(
                    or_(Account.nickname.like(pattern), Account.remark.like(pattern))
                )

            count_stmt = select(func.count()).select_from(Account)
            list_stmt = select(Account).order_by(Account.platform.asc(), Account.id.asc())
            for condition in conditions:
                count_stmt = count_stmt.where(condition)
                list_stmt = list_stmt.where(condition)

            total = self.db.execute(count_stmt).scalar_one()
            items = list(self.db.execute(list_stmt).scalars().all())
            return items, int(total)
        except SQLAlchemyError as exc:
            logger.exception("查询账号列表失败")
            raise DatabaseError("查询账号列表失败") from exc

    def get_account(self, account_id: int) -> Account:
        """按 ID 获取账号。

        Raises:
            NotFoundError: 账号不存在。
            DatabaseError: 数据库查询异常。
        """
        try:
            account = self.db.get(Account, account_id)
        except SQLAlchemyError as exc:
            logger.exception("查询账号失败 | id=%s", account_id)
            raise DatabaseError("查询账号失败") from exc

        if account is None:
            raise NotFoundError(f"账号不存在：id={account_id}")
        return account

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def create_account(self, payload: AccountCreate) -> Account:
        """创建账号。

        Raises:
            ConflictError: 同一平台下昵称已存在。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                account = Account(
                    platform=payload.platform,
                    nickname=payload.nickname,
                    account_uid=payload.account_uid,
                    status=payload.status,
                    remark=payload.remark,
                )
                self.db.add(account)
                # flush 触发唯一约束校验
                self.db.flush()

            logger.info(
                "账号创建成功 | id=%s | platform=%s | nickname=%s",
                account.id,
                account.platform,
                account.nickname,
            )
            return account
        except IntegrityError as exc:
            logger.warning(
                "账号创建冲突 | platform=%s | nickname=%s", payload.platform, payload.nickname
            )
            raise ConflictError(
                f"平台「{payload.platform}」下已存在昵称为「{payload.nickname}」的账号"
            ) from exc
        except SQLAlchemyError as exc:
            logger.exception("账号创建失败 | nickname=%s", payload.nickname)
            raise DatabaseError("账号创建失败") from exc

    def update_account(self, account_id: int, payload: AccountUpdate) -> Account:
        """更新账号，仅更新显式传入的字段。

        Raises:
            NotFoundError: 账号不存在。
            ConflictError: 修改后与同平台其他账号昵称冲突。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                account = self.db.get(Account, account_id)
                if account is None:
                    raise NotFoundError(f"账号不存在：id={account_id}")

                for field, value in payload.model_dump(exclude_unset=True).items():
                    if value is not None:
                        setattr(account, field, value)

                self.db.flush()

            logger.info("账号更新成功 | id=%s", account_id)
            return account
        except (NotFoundError, ConflictError):
            raise
        except IntegrityError as exc:
            logger.warning("账号更新冲突 | id=%s", account_id)
            raise ConflictError("修改后的平台与昵称组合已被其他账号占用") from exc
        except SQLAlchemyError as exc:
            logger.exception("账号更新失败 | id=%s", account_id)
            raise DatabaseError("账号更新失败") from exc

    def delete_account(self, account_id: int) -> None:
        """删除账号。

        存在未完成的发布任务时拒绝删除，避免产生指向已删账号的悬挂任务。

        Raises:
            NotFoundError: 账号不存在。
            ConflictError: 账号下仍有未完成任务。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                account = self.db.get(Account, account_id)
                if account is None:
                    raise NotFoundError(f"账号不存在：id={account_id}")

                unfinished = self.db.execute(
                    select(func.count())
                    .select_from(PublishTask)
                    .where(
                        PublishTask.account_id == account_id,
                        PublishTask.status.in_(
                            (PublishTaskStatus.PENDING, PublishTaskStatus.RUNNING)
                        ),
                    )
                ).scalar_one()

                if unfinished:
                    raise ConflictError(
                        f"该账号还有 {unfinished} 个未完成的发布任务，请先取消或等待完成后再删除"
                    )

                self.db.delete(account)

            logger.info("账号删除成功 | id=%s", account_id)
        except (NotFoundError, ConflictError):
            raise
        except IntegrityError as exc:
            # 存在历史任务引用时外键约束会拦截
            logger.warning("账号删除被外键约束拒绝 | id=%s", account_id)
            raise ConflictError("该账号存在关联的发布任务记录，无法删除") from exc
        except SQLAlchemyError as exc:
            logger.exception("账号删除失败 | id=%s", account_id)
            raise DatabaseError("账号删除失败") from exc
