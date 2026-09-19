"""Cookie 库业务逻辑层。

所有写操作包裹在事务中，失败整体回滚。重名（同平台同名）不做先查后插，
靠 model 层唯一约束 + IntegrityError 转 409 —— 先查后插在并发下有竞态窗口。
"""

from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.db.session import transaction
from app.models.crawl_cookie import CrawlCookie
from app.schemas.crawl_cookie import CrawlCookieCreate, CrawlCookieUpdate

logger = get_logger(__name__)


class CrawlCookieService:
    """Cookie 库服务。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_cookies(self, *, platform: Optional[str] = None) -> Tuple[List[CrawlCookie], int]:
        """查询 Cookie 列表（量少不分页，一次返回全部）。

        Args:
            platform: 按平台过滤。

        Returns:
            (Cookie 列表, 总条数)
        """
        try:
            stmt = select(CrawlCookie).order_by(
                CrawlCookie.platform.asc(), CrawlCookie.created_at.desc()
            )
            if platform:
                stmt = stmt.where(CrawlCookie.platform == platform.strip().lower())
            items = list(self.db.execute(stmt).scalars().all())
            return items, len(items)
        except SQLAlchemyError as exc:
            logger.exception("查询 Cookie 列表失败")
            raise DatabaseError("查询 Cookie 列表失败") from exc

    def get_cookie(self, cookie_id: int) -> CrawlCookie:
        """按 ID 获取 Cookie。

        Raises:
            NotFoundError: Cookie 不存在。
            DatabaseError: 数据库查询异常。
        """
        try:
            cookie = self.db.get(CrawlCookie, cookie_id)
        except SQLAlchemyError as exc:
            logger.exception("查询 Cookie 失败 | id=%s", cookie_id)
            raise DatabaseError("查询 Cookie 失败") from exc

        if cookie is None:
            raise NotFoundError(f"Cookie 不存在：id={cookie_id}")
        return cookie

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def create_cookie(self, payload: CrawlCookieCreate) -> CrawlCookie:
        """保存一条 Cookie。

        Raises:
            ConflictError: 同一平台下该名称已存在。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                cookie = CrawlCookie(
                    platform=payload.platform,
                    name=payload.name,
                    cookie=payload.cookie,
                    remark=payload.remark,
                )
                self.db.add(cookie)
                # flush 触发唯一约束校验
                self.db.flush()

            logger.info(
                "Cookie 保存成功 | id=%s | platform=%s | name=%s",
                cookie.id,
                cookie.platform,
                cookie.name,
            )
            return cookie
        except IntegrityError as exc:
            logger.warning(
                "Cookie 保存冲突 | platform=%s | name=%s", payload.platform, payload.name
            )
            raise ConflictError(
                f"平台「{payload.platform}」下已存在同名 Cookie：{payload.name}"
            ) from exc
        except SQLAlchemyError as exc:
            logger.exception("Cookie 保存失败 | name=%s", payload.name)
            raise DatabaseError("Cookie 保存失败") from exc

    def update_cookie(self, cookie_id: int, payload: CrawlCookieUpdate) -> CrawlCookie:
        """更新 Cookie，仅更新显式传入的字段。

        Raises:
            NotFoundError: Cookie 不存在。
            ConflictError: 修改后与其他 Cookie 的平台+名称组合冲突。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                cookie = self.db.get(CrawlCookie, cookie_id)
                if cookie is None:
                    raise NotFoundError(f"Cookie 不存在：id={cookie_id}")

                for field, value in payload.model_dump(exclude_unset=True).items():
                    if value is not None:
                        setattr(cookie, field, value)

                self.db.flush()

            logger.info("Cookie 更新成功 | id=%s", cookie_id)
            return cookie
        except (NotFoundError, ConflictError):
            raise
        except IntegrityError as exc:
            logger.warning("Cookie 更新冲突 | id=%s", cookie_id)
            raise ConflictError("修改后的平台与名称组合已被其他 Cookie 占用") from exc
        except SQLAlchemyError as exc:
            logger.exception("Cookie 更新失败 | id=%s", cookie_id)
            raise DatabaseError("Cookie 更新失败") from exc

    def delete_cookie(self, cookie_id: int) -> None:
        """删除 Cookie。

        Raises:
            NotFoundError: Cookie 不存在。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                cookie = self.db.get(CrawlCookie, cookie_id)
                if cookie is None:
                    raise NotFoundError(f"Cookie 不存在：id={cookie_id}")

                self.db.delete(cookie)

            logger.info("Cookie 删除成功 | id=%s", cookie_id)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("Cookie 删除失败 | id=%s", cookie_id)
            raise DatabaseError("Cookie 删除失败") from exc
