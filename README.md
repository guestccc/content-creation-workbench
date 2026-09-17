# 内容创作工作台

面向内容创作者的选题、创作与发布管理工作台。采用前后端分离的单仓库（monorepo）结构。

## 技术栈

| 模块 | 技术选型 | 说明 |
| --- | --- | --- |
| 后端 | FastAPI + SQLAlchemy 2.0 + Pydantic v2 | 自带 OpenAPI 交互式文档 |
| 数据库 | SQLite（默认） | 通过 `DATABASE_URL` 可切换 PostgreSQL 等 |
| 前端 | React 18 + TypeScript + Vite 5 | 单页应用，开发环境自动代理接口 |
| 后端包管理 | [uv](https://docs.astral.sh/uv/) | 依赖锁定与虚拟环境管理 |
| 前端包管理 | npm | |

## 目录结构

```
内容创作工作台/
├── backend/                  # 后端服务（Python / FastAPI）
│   ├── app/
│   │   ├── api/              # 路由层与依赖注入
│   │   │   ├── deps.py       # 公共依赖（数据库会话、服务实例）
│   │   │   └── v1/           # v1 版本接口
│   │   ├── core/             # 配置、日志、异常与全局异常处理器
│   │   ├── db/               # 引擎、会话、事务、初始化
│   │   ├── models/           # SQLAlchemy ORM 模型
│   │   ├── schemas/          # Pydantic 请求/响应模型
│   │   ├── services/         # 业务逻辑（事务边界所在层）
│   │   └── main.py           # 应用入口
│   ├── tests/                # 接口测试与单元测试
│   ├── pyproject.toml        # 依赖与工具配置
│   └── README.md
│
├── frontend/                 # 前端应用（React / TypeScript）
│   ├── src/
│   │   ├── api/              # 接口封装（统一错误处理）
│   │   ├── components/       # 通用组件
│   │   ├── pages/            # 页面
│   │   ├── types/            # 类型定义
│   │   ├── utils/            # 工具函数
│   │   └── main.tsx          # 入口
│   ├── vite.config.ts        # 构建与开发代理配置
│   └── README.md
│
└── README.md                 # 本文件
```

## 快速开始

### 环境要求

| 依赖 | 版本要求 |
| --- | --- |
| Python | 3.11 及以上（已在 3.14 验证） |
| Node.js | 18 及以上（已在 18.20 验证） |
| uv | 最新版即可 |

> **关于 Node 版本**：当前前端锁定 Vite 5 + React 18，在 Node 18 / 20 / 22 上均可运行。
> 注意 Node 18 已于 2025 年 4 月停止维护，建议尽早升级到 Node 20 或 22 LTS。

### 1. 启动后端

```bash
cd backend

# 安装依赖（自动创建 .venv）
uv sync

# 准备环境变量（可选，不配置也能以默认值启动）
cp .env.example .env

# 启动开发服务器（默认 http://127.0.0.1:8000）
uv run uvicorn app.main:app --reload
```

启动后可直接访问交互式接口文档：

- Swagger UI：<http://127.0.0.1:8000/docs>
- ReDoc：<http://127.0.0.1:8000/redoc>

### 2. 启动前端

```bash
cd frontend

# 安装依赖
npm install

# 启动开发服务器（默认 http://127.0.0.1:5173）
npm run dev
```

浏览器打开 <http://127.0.0.1:5173> 即可。

开发环境下，前端的 `/api` 请求会由 Vite 自动代理到 `http://127.0.0.1:8000`，
因此**无需额外配置跨域**，两边各自启动即可联调。

## 运行测试

```bash
cd backend
uv run pytest
```

测试使用独立的内存数据库，不会触碰开发环境的数据文件。

## 接口一览

所有接口以 `/api/v1` 为前缀，统一响应结构如下：

成功：

```json
{ "success": true, "data": { } }
```

失败：

```json
{ "success": false, "error": { "code": "NOT_FOUND", "message": "内容不存在：id=1" } }
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 健康检查（含数据库连通性） |
| GET | `/api/v1/contents` | 分页查询内容列表，支持关键字、状态、平台过滤 |
| POST | `/api/v1/contents` | 创建内容 |
| GET | `/api/v1/contents/statistics` | 内容统计（总量与各状态分布） |
| GET | `/api/v1/contents/{id}` | 获取内容详情 |
| PUT | `/api/v1/contents/{id}` | 更新内容（仅更新传入的字段） |
| DELETE | `/api/v1/contents/{id}` | 删除内容 |

### 错误码约定

| code | HTTP 状态码 | 含义 |
| --- | --- | --- |
| `VALIDATION_ERROR` | 422 | 请求参数校验失败，`error.details` 含字段级明细 |
| `NOT_FOUND` | 404 | 请求的资源不存在 |
| `CONFLICT` | 409 | 资源冲突 |
| `DATABASE_ERROR` | 500 | 数据库操作失败 |
| `INTERNAL_ERROR` | 500 | 未预期的服务端异常 |

## 设计约定

- **异常处理**：所有接口异常统一由全局处理器转换为标准响应体，服务端异常不会向客户端泄露堆栈信息。
- **事务**：所有写操作包裹在 `transaction()` 上下文中，失败整体回滚，不会产生半截数据。
- **分层**：路由层只做参数校验与响应组装，业务逻辑集中在 `services/`，数据访问集中在 `models/`。
- **配置**：所有可变配置通过环境变量注入，敏感信息不进入代码仓库。

## 后续可扩展方向

- 接入 Alembic 管理数据库迁移（当前开发阶段用 `create_all` 建表）
- 增加用户体系与权限控制
- 接入大模型能力，实现 AI 辅助选题与文案生成
- 增加内容发布状态流转与定时发布
