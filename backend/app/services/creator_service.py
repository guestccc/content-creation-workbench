"""创作者主页业务逻辑层。

所有写操作包裹在事务中，失败整体回滚。重名（同平台同主页）不做先查后插，
靠 model 层唯一约束 + IntegrityError 转 409 —— 先查后插在并发下有竞态窗口。
"""

from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, DatabaseError, NotFoundError
from app.core.logging import get_logger
from app.db.session import transaction
from app.models.creator import Creator
from app.schemas.creator import CreatorCreate, CreatorUpdate

logger = get_logger(__name__)


class CreatorService:
    """创作者主页服务。"""

    def __init__(self, db: Session) -> None:
        """初始化服务。

        Args:
            db: 由依赖注入提供的数据库会话。
        """
        self.db = db

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_creators(
        self,
        *,
        platform: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> Tuple[List[Creator], int]:
        """查询创作者列表（量少不分页，一次返回全部）。

        Args:
            platform: 按平台过滤。
            tag: 按标签过滤（tags 存 JSON 列，条目级匹配在 Python 侧做，
                SQLite 的 JSON 查询函数不值得为这点数据量引入）。

        Returns:
            (创作者列表, 总条数)
        """
        try:
            stmt = select(Creator).order_by(Creator.created_at.desc(), Creator.id.desc())
            if platform:
                stmt = stmt.where(Creator.platform == platform.strip().lower())
            items = list(self.db.execute(stmt).scalars().all())

            if tag:
                cleaned = tag.strip()
                items = [item for item in items if cleaned in (item.tags or [])]

            return items, len(items)
        except SQLAlchemyError as exc:
            logger.exception("查询创作者列表失败")
            raise DatabaseError("查询创作者列表失败") from exc

    def get_creator(self, creator_id: int) -> Creator:
        """按 ID 获取创作者。

        Raises:
            NotFoundError: 创作者不存在。
            DatabaseError: 数据库查询异常。
        """
        try:
            creator = self.db.get(Creator, creator_id)
        except SQLAlchemyError as exc:
            logger.exception("查询创作者失败 | id=%s", creator_id)
            raise DatabaseError("查询创作者失败") from exc

        if creator is None:
            raise NotFoundError(f"创作者不存在：id={creator_id}")
        return creator

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def create_creator(self, payload: CreatorCreate) -> Creator:
        """创建创作者。

        Raises:
            ConflictError: 同一平台下该主页已存在。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                creator = Creator(
                    platform=payload.platform,
                    name=payload.name,
                    homepage=payload.homepage,
                    tags=payload.tags,
                    remark=payload.remark,
                )
                self.db.add(creator)
                # flush 触发唯一约束校验
                self.db.flush()

            logger.info(
                "创作者创建成功 | id=%s | platform=%s | name=%s",
                creator.id,
                creator.platform,
                creator.name,
            )
            return creator
        except IntegrityError as exc:
            logger.warning(
                "创作者创建冲突 | platform=%s | homepage=%s", payload.platform, payload.homepage
            )
            raise ConflictError(
                f"平台「{payload.platform}」下已存在该创作者主页：{payload.homepage[:80]}"
            ) from exc
        except SQLAlchemyError as exc:
            logger.exception("创作者创建失败 | name=%s", payload.name)
            raise DatabaseError("创作者创建失败") from exc

    def update_creator(self, creator_id: int, payload: CreatorUpdate) -> Creator:
        """更新创作者，仅更新显式传入的字段。

        Raises:
            NotFoundError: 创作者不存在。
            ConflictError: 修改后与其他创作者的平台+主页组合冲突。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                creator = self.db.get(Creator, creator_id)
                if creator is None:
                    raise NotFoundError(f"创作者不存在：id={creator_id}")

                for field, value in payload.model_dump(exclude_unset=True).items():
                    if value is not None:
                        setattr(creator, field, value)

                self.db.flush()

            logger.info("创作者更新成功 | id=%s", creator_id)
            return creator
        except (NotFoundError, ConflictError):
            raise
        except IntegrityError as exc:
            logger.warning("创作者更新冲突 | id=%s", creator_id)
            raise ConflictError("修改后的平台与主页组合已被其他创作者占用") from exc
        except SQLAlchemyError as exc:
            logger.exception("创作者更新失败 | id=%s", creator_id)
            raise DatabaseError("创作者更新失败") from exc

    def delete_creator(self, creator_id: int) -> None:
        """删除创作者。

        抓取任务的 params 只是入库时的快照（无外键），删创作者不影响历史任务。

        Raises:
            NotFoundError: 创作者不存在。
            DatabaseError: 写库失败（事务已回滚）。
        """
        try:
            with transaction(self.db):
                creator = self.db.get(Creator, creator_id)
                if creator is None:
                    raise NotFoundError(f"创作者不存在：id={creator_id}")

                self.db.delete(creator)

            logger.info("创作者删除成功 | id=%s", creator_id)
        except NotFoundError:
            raise
        except SQLAlchemyError as exc:
            logger.exception("创作者删除失败 | id=%s", creator_id)
            raise DatabaseError("创作者删除失败") from exc
