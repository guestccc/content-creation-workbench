"""数据库初始化。

开发阶段直接用 create_all 建表；生产环境应改用 Alembic 迁移管理表结构变更。
"""

from typing import Optional

from sqlalchemy import Column, inspect, text

from app.core.logging import get_logger
from app.db.base import Base
from app.db.session import engine

# 导入模型包，确保所有 ORM 类都注册到 Base.metadata 上
import app.models  # noqa: F401

logger = get_logger(__name__)


def init_db() -> None:
    """创建所有尚不存在的数据表（已存在的表不会被修改），并补齐新增的列。"""
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
    logger.info("数据库表结构初始化完成")


def _render_default(column: Column) -> Optional[str]:
    """把一个 Python 侧的默认值渲染成能写进 DDL 的字面量。

    SQLite 的 `ALTER TABLE ADD COLUMN` 加非空列时**必须**给 DEFAULT，
    否则语句直接被拒。这里只认能安全内联的标量（字符串 / 数字 / 布尔），
    其余（可调用默认值如 utcnow、序列默认值如 list）返回 None，
    由调用方跳过并告警 —— 猜错了会写坏数据，宁可留一列不加。

    Returns:
        可直接拼进 DDL 的字面量（含引号），渲染不了时返回 None。
    """
    default = column.default
    if default is None or default.is_callable or default.is_sequence:
        return None

    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def _add_missing_columns() -> int:
    """给已存在的表补上模型里新加的列。

    为什么需要这个：`create_all` 只建**表**，不改**表**。老库（比如升级前
    建好的 workbench.db）缺新列时，SQLAlchemy 一查询就报 no such column，
    整个镜头分割功能直接不可用 —— 而库里还有用户的任务历史，不能删。

    只做 ADD COLUMN：不删列、不改类型、不动数据，可以反复执行（已存在的
    列直接跳过）。上 Alembic 之后这个函数应该整个删掉，换成迁移脚本。

    Returns:
        本次补上的列数。
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added = 0

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # 新建的表由 create_all 负责，列一定齐

        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue

            ddl_type = column.type.compile(dialect=engine.dialect)
            if column.nullable:
                clause = ""
            else:
                literal = _render_default(column)
                if literal is None:
                    logger.warning(
                        "列非空但没有可安全内联的默认值，跳过补列 | %s.%s",
                        table.name,
                        column.name,
                    )
                    continue
                clause = f" NOT NULL DEFAULT {literal}"

            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            f'ALTER TABLE "{table.name}" '
                            f'ADD COLUMN "{column.name}" {ddl_type}{clause}'
                        )
                    )
            except Exception:  # noqa: BLE001 - 补列失败要让服务带着完整信息起来
                logger.exception("补列失败 | %s.%s", table.name, column.name)
                continue

            logger.info(
                "补齐缺失的列 | %s.%s %s%s", table.name, column.name, ddl_type, clause
            )
            added += 1

    if added:
        logger.info("共补齐 %s 个缺失的列", added)
    return added
