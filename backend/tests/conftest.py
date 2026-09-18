"""pytest 全局夹具。

核心目标：让测试跑在独立的内存数据库上，绝不触碰开发环境的 workbench.db。
实现方式是覆盖 FastAPI 的 get_db 依赖，替换为测试专用会话。
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 导入模型包，确保建表时元数据完整
import app.models  # noqa: F401
from app.db.base import Base
from app.db.session import get_db
from app.main import app

# SQLite 内存库：StaticPool 让所有连接复用同一个内存实例，
# 否则每个新连接都会得到一个空库，测试必然失败。
TEST_DATABASE_URL = "sqlite://"

test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

@event.listens_for(test_engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
    """SQLite 默认不校验外键，这里手动打开。

    必须与 app/db/session.py 保持一致，否则「外键约束保护删除」这类
    依赖数据库约束的用例在测试里会静默失效。
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


TestingSessionLocal = sessionmaker(
    bind=test_engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


@pytest.fixture()
def db_session():
    """提供独立的内存数据库会话，用例结束后销毁表结构。"""
    Base.metadata.create_all(bind=test_engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=test_engine)


@pytest.fixture()
def client(db_session):
    """提供覆盖了数据库依赖的测试客户端。

    注意不使用 with 语法，避免触发 lifespan 里的 init_db() 在真实库上建表。
    """

    def _override_get_db():
        """用测试会话替换真实数据库会话。"""
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """把「主目录」指到临时目录，供 ~ 展开的用例使用。

    跨平台：POSIX 的 os.path.expanduser 认 HOME；Windows 认 USERPROFILE
    （实测 Windows 上 HOME 会被忽略）。返回临时目录路径。
    """
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
    else:
        monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def sample_content_payload() -> dict:
    """一份标准的内容创建请求体，供各用例复用。"""
    return {
        "title": "秋季新品保温杯种草文案",
        "body": "316 不锈钢内胆，保温 12 小时，办公室必备。",
        "platform": "抖音",
        "status": "draft",
        "tags": ["保温杯", "办公好物"],
        "author": "小明",
    }


@pytest.fixture()
def created_content(client, sample_content_payload) -> dict:
    """预先创建一条内容，返回接口响应中的 data 部分。"""
    response = client.post("/api/v1/contents", json=sample_content_payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


@pytest.fixture()
def sample_account_payload() -> dict:
    """一份标准的账号创建请求体，供各用例复用。"""
    return {
        "platform": "抖音",
        "nickname": "小明的好物分享",
        "account_uid": "dy_10086",
        "status": "active",
        "remark": "主账号",
    }


@pytest.fixture()
def created_account(client, sample_account_payload) -> dict:
    """预先创建一个账号，返回接口响应中的 data 部分。"""
    response = client.post("/api/v1/accounts", json=sample_account_payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


@pytest.fixture()
def create_account(client):
    """返回一个「创建账号」的工厂函数，便于一次用例中建多个账号。"""

    def _create(platform: str = "抖音", nickname: str = "测试账号", **extra) -> dict:
        payload = {"platform": platform, "nickname": nickname, **extra}
        response = client.post("/api/v1/accounts", json=payload)
        assert response.status_code == 201, response.text
        return response.json()["data"]

    return _create


@pytest.fixture()
def create_task(client):
    """返回一个「创建发布任务」的工厂函数。"""

    def _create(content_id: int, account_id: int, **extra) -> dict:
        payload = {"content_id": content_id, "account_id": account_id, **extra}
        response = client.post("/api/v1/publish-tasks", json=payload)
        assert response.status_code == 201, response.text
        return response.json()["data"]

    return _create
