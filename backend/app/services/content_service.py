"""内容业务逻辑层。

约定：
1. 所有写操作都包裹在 transaction() 事务上下文中，失败整体回滚；
2. 数据库异常统一转换为 DatabaseError，向上由全局处理器兜底；
3. 业务规则（如资源不存在）抛出 NotFoundError 等业务异常。
"""

from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.exceptions import DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.db.session import transaction
from app.models.content import Content, ContentStatus
from app.schemas.content import ContentCreate, ContentUpdate

logger = get_logger(__name__)


class ContentService:
    """内容服务：封装内容相关的全部数据操作。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_contents(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None,
        platform: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> Tuple[List[Content], int]:
        """分页查询内容列表。

        Args:
            page: 页码，从 1 开始。
            page_size: 每页条数。
            status: 按状态过滤，为空则不过滤。
            platform: 按平台过滤，为空则不过滤。
            keyword: 标题或正文模糊匹配关键字。

        Returns:
            (当前页数据列表, 满足条件的总条数)
        """
        try:
            conditions = []
            if status:
                conditions.append(Content.status == status)
            if platform:
                conditions.append(Content.platform == platform)
            if keyword:
                pattern = f"%{keyword}%"
                conditions.append(
                    or_(Content.title.like(pattern), Content.body.like(pattern))
                )

            count_stmt = select(func.count()).select_from(Content)
            list_stmt = select(Content)
            for condition in conditions:
                count_stmt = count_stmt.where(condition)
                list_stmt = list_stmt.where(condition)

            total = self.db.execute(count_stmt).scalar_one()

            list_stmt = (
                list_stmt.order_by(Content.updated_at.desc(), Content.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            items = list(self.db.execute(list_stmt).scalars().all())

            return items, int(total)
        except SQLAlchemyError as exc:
            logger.exception("查询内容列表失败")
            raise DatabaseError("查询内容列表失败") from exc

    def get_content(self, content_id: int) -> Content:
        """按 ID 获取内容。

        Raises:
            NotFoundError: 内容不存在。
            DatabaseError: 数据库查询异常。
        """
        try:
            content = self.db.get(Content, content_id)
        except SQLAlchemyError as exc:
            logger.exception("查询内容失败 | id=%s", content_id)
            raise DatabaseError("查询内容失败") from exc

        if content is None:
            raise NotFoundError(f"内容不存在：id={content_id}")
        return content

    def get_statistics(self) -> Dict:
        """统计内容总量与各状态分布，供工作台首页概览使用。"""
        try:
            stmt = select(Content.status, func.count(Content.id)).group_by(Content.status)
            rows = self.db.execute(stmt).all()

            # 先铺满所有状态，保证前端无需处理字段缺失
            by_status: Dict[str, int] = {status: 0 for status in ContentStatus.ALL}
            for status, count in rows:
                by_status[status] = int(count)

            return {"total": sum(by_status.values()), "by_status": by_status}
        except SQLAlchemyError as exc:
            logger.exception("统计内容失败")
            raise DatabaseError("统计内容失败") from exc

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def create_content(self, payload: ContentCreate) -> Content:
        """创建内容。

        Raises:
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                content = Content(
                    title=payload.title,
                    body=payload.body,
                    platform=payload.platform,
                    status=payload.status,
                    tags=",".join(payload.tags),
                    author=payload.author,
                )
                self.db.add(content)
                # flush 既触发数据库约束校验，又能拿到自增主键
                self.db.flush()

            logger.info("内容创建成功 | id=%s | title=%s", content.id, content.title)
            return content
        except SQLAlchemyError as exc:
            logger.exception("内容创建失败 | title=%s", payload.title)
            raise DatabaseError("内容创建失败") from exc

    def update_content(self, content_id: int, payload: ContentUpdate) -> Content:
        """更新内容，仅更新显式传入的字段。

        Raises:
            NotFoundError: 内容不存在。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                content = self.db.get(Content, content_id)
                if content is None:
                    raise NotFoundError(f"内容不存在：id={content_id}")

                # exclude_unset 保证未传字段保持原值；传 null 的字段同样跳过
                updates = payload.model_dump(exclude_unset=True)
                for field, value in updates.items():
                    if value is None:
                        continue
                    if field == "tags":
                        # 标签以逗号分隔字符串入库
                        content.tags = ",".join(value)
                    else:
                        setattr(content, field, value)

                self.db.flush()

            logger.info("内容更新成功 | id=%s", content_id)
            return content
        except NotFoundError:
            # 业务异常直接向上传递，无需包装
            raise
        except SQLAlchemyError as exc:
            logger.exception("内容更新失败 | id=%s", content_id)
            raise DatabaseError("内容更新失败") from exc

    def delete_content(self, content_id: int) -> None:
        """删除内容。

        Raises:
            NotFoundError: 内容不存在。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                content = self.db.get(Content, content_id)
                if content is None:
                    raise NotFoundError(f"内容不存在：id={content_id}")
                self.db.delete(content)

            logger.info("内容删除成功 | id=%s", content_id)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("内容删除失败 | id=%s", content_id)
            raise DatabaseError("内容删除失败") from exc
