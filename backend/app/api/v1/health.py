"""系统健康检查接口。"""

from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import DbSession
from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.common import ApiResponse, HealthData

router = APIRouter(tags=["系统"])
logger = get_logger(__name__)


@router.get("/health", response_model=ApiResponse[HealthData], summary="健康检查")
def health_check(db: DbSession) -> ApiResponse[HealthData]:
    """检查服务存活状态与数据库连通性。

    数据库不可用时不让接口整体失败，而是返回 degraded 状态，
    便于运维通过状态字段区分「服务挂了」和「数据库挂了」。
    """
    try:
        db.execute(text("SELECT 1"))
        database_ok = True
    except SQLAlchemyError:
        logger.exception("健康检查：数据库连接失败")
        database_ok = False

    return ApiResponse(
        data=HealthData(
            status="ok" if database_ok else "degraded",
            app_name=settings.APP_NAME,
            version=settings.APP_VERSION,
            database="connected" if database_ok else "disconnected",
        )
    )
