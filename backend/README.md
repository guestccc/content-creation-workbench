# content-creation-workbench · 后端服务

基于 FastAPI 的 content-creation-workbench 后端，提供内容的增删改查与统计接口。

## 技术选型

| 组件 | 版本要求 | 用途 |
| --- | --- | --- |
| Python | >= 3.11 | 运行环境（已在 3.14 验证） |
| FastAPI | >= 0.115 | Web 框架 |
| SQLAlchemy | >= 2.0.36 | ORM |
| Pydantic | >= 2.9 | 数据校验与序列化 |
| pydantic-settings | >= 2.6 | 配置管理 |
| pytest + httpx | 开发依赖 | 测试 |

## 快速开始

```bash
# 安装依赖（自动创建 .venv）
uv sync

# 配置环境变量（可选）
cp .env.example .env

# 启动开发服务器
uv run uvicorn app.main:app --reload
```

服务默认监听 `http://127.0.0.1:8000`，接口文档：

- Swagger UI：<http://127.0.0.1:8000/docs>
- ReDoc：<http://127.0.0.1:8000/redoc>

## 目录结构

```
backend/
├── app/
│   ├── api/
│   │   ├── deps.py           # 依赖注入：数据库会话、服务实例
│   │   └── v1/
│   │       ├── router.py     # 路由聚合，新增模块在此注册
│   │       ├── health.py     # 健康检查
│   │       └── contents.py   # 内容管理接口
│   ├── core/
│   │   ├── config.py         # 配置（环境变量 / .env）
│   │   ├── logging.py        # 日志配置
│   │   ├── exceptions.py     # 业务异常定义
│   │   └── exception_handlers.py  # 全局异常处理器
│   ├── db/
│   │   ├── base.py           # DeclarativeBase
│   │   ├── session.py        # 引擎、会话、transaction() 事务上下文
│   │   └── init_db.py        # 建表
│   ├── models/content.py     # ORM 模型
│   ├── schemas/              # Pydantic 请求/响应模型
│   ├── services/content_service.py  # 业务逻辑（事务边界）
│   └── main.py               # 应用入口
└── tests/                    # 测试
```

## 运行测试

```bash
uv run pytest
```

测试特点：

- 使用 **内存数据库**（`sqlite://` + `StaticPool`），不会触碰开发环境的 `workbench.db`；
- 通过 `app.dependency_overrides` 替换数据库依赖，测试间互不干扰；
- 覆盖正常流程、参数校验、资源不存在、分页过滤等分支。

## 配置项

所有配置项均可通过环境变量或 `.env` 文件覆盖，完整列表见 `.env.example`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_NAME` | content-creation-workbench | 应用名称 |
| `DEBUG` | false | 调试模式 |
| `HOST` / `PORT` | 127.0.0.1 / 8000 | 监听地址 |
| `DATABASE_URL` | sqlite:///./workbench.db | 数据库连接串 |
| `DB_ECHO` | false | 是否打印 SQL |
| `CORS_ORIGINS` | localhost:5173 | 跨域白名单，逗号分隔 |
| `LOG_LEVEL` | INFO | 日志级别 |

切换数据库只需修改 `DATABASE_URL`，例如：

```
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/workbench
```

## 架构约定

### 分层职责

| 层 | 职责 | 不应出现 |
| --- | --- | --- |
| `api/` | 参数校验、调用服务、组装响应 | 业务逻辑、直接操作数据库 |
| `services/` | 业务逻辑、事务边界 | HTTP 相关代码 |
| `models/` | ORM 映射 | 业务规则 |
| `schemas/` | 入参校验、出参序列化 | 数据库查询 |

### 异常处理

所有异常由 `core/exception_handlers.py` 统一转换为标准响应体：

```json
{ "success": false, "error": { "code": "NOT_FOUND", "message": "内容不存在：id=1" } }
```

- 业务异常继承 `AppException`，自带 `code` 与 `status_code`；
- 数据库异常记录完整堆栈，对外只返回通用提示；
- 兜底处理器捕获所有未预期异常，**不向前端泄露堆栈信息**。

### 事务

所有写操作必须包裹在 `transaction()` 中：

```python
from app.db.session import transaction

with transaction(self.db):
    self.db.add(content)
    self.db.flush()   # 触发约束校验并获取自增主键
# 正常退出自动 commit，异常自动 rollback
```

## 新增一个业务模块

以「素材管理」为例：

1. 在 `app/models/` 新增 ORM 模型，并在 `app/models/__init__.py` 导出（确保建表时被收集）；
2. 在 `app/schemas/` 定义请求与响应模型；
3. 在 `app/services/` 实现业务逻辑，写操作使用 `transaction()`；
4. 在 `app/api/v1/` 新增路由文件，然后在 `router.py` 中 `include_router`；
5. 在 `tests/` 补充测试。

## 后续可扩展方向

- 引入 Alembic 管理表结构变更（当前用 `create_all`，适合开发阶段）
- 引入依赖注入容器与用户鉴权
- 引入后台任务队列处理耗时操作（如视频转码、批量生成）
