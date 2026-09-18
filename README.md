# 内容创作工作台

面向内容创作者的选题、创作与发布管理工作台。采用前后端分离的单仓库（monorepo）结构。

包含三个子项目：

| 子项目 | 定位 |
| --- | --- |
| `backend/` | 业务后端：内容管理、账号登记、发布任务排队 |
| `frontend/` | Web 工作台：内容创作与任务管理界面 |
| `desktop/` | 桌面客户端：**代理发布**，在本地持有凭证并执行发布动作 |

## 技术栈

| 模块 | 技术选型 | 说明 |
| --- | --- | --- |
| 后端 | FastAPI + SQLAlchemy 2.0 + Pydantic v2 | 自带 OpenAPI 交互式文档 |
| 数据库 | SQLite（默认） | 通过 `DATABASE_URL` 可切换 PostgreSQL 等 |
| 前端 | React 18 + TypeScript + Vite 5 + antd 6 | 单页应用，开发环境自动代理接口；界面组件统一用 antd，约定见 `frontend/README.md` |
| 桌面客户端 | Electron + electron-vite + React 18 | 主进程持有凭证并执行发布，渲染进程无 Node 权限 |
| 后端包管理 | [uv](https://docs.astral.sh/uv/) | 依赖锁定与虚拟环境管理 |
| 前端 / 客户端包管理 | npm | |

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
├── frontend/                 # Web 工作台（React / TypeScript）
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
├── desktop/                  # 桌面客户端（Electron / React）
│   ├── src/
│   │   ├── main/             # 主进程：窗口、IPC、调度器、凭证存储
│   │   │   ├── ipc/          # IPC 处理器
│   │   │   ├── publishers/   # 平台发布器适配器
│   │   │   └── services/     # 配置、接口客户端、凭证、调度器
│   │   ├── preload/          # contextBridge 桥接
│   │   ├── renderer/         # 渲染进程（React 界面）
│   │   └── shared/           # 三端共用的类型与常量
│   ├── electron.vite.config.ts
│   └── README.md
│
├── cli/                      # 开发服务管理 CLI（Python / Typer）
│   ├── src/cw/               # 菜单、进程管理、就绪探测、日志
│   ├── tests/                # 单元测试
│   ├── scripts/              # 伪终端端到端验收脚本
│   └── README.md
│
├── cw                        # CLI 启动器（./cw 直接使用）
│
├── materials/                # 素材目录：按流程分四段，只有骨架与说明入库
│   ├── README.md             # 每一段放什么（见下）
│   ├── source/               # ① 原始素材：待切的视频往这里拷
│   ├── clips/                # ② 镜头分割产物：每条原片一个 <视频名>_scenes/
│   ├── subtitle/             # ③ 字幕与文案产物（.srt / .txt）
│   └── output/               # ④ 成片，待发布
│
└── README.md                 # 本文件
```

## 代理发布是怎么工作的

发布动作不在服务端执行，而是由桌面客户端在本机完成：

```
Web 工作台创建内容
        │
        ▼
后端：把内容 × 账号 拆成发布任务，状态置为「排队中」
        │
        ▼  客户端定时认领
客户端：任务转为「发布中」，解密本机凭证 → 调用平台适配器发布
        │
        ▼  回报结果
后端：成功则归档并记录链接；失败且未超重试上限则退回队列
```

这样设计的原因：平台登录凭证必须留在用户自己的机器上。
后端只保存账号元信息（平台、昵称、状态），**从不接触任何凭证**；
凭证由客户端用系统加密（macOS 钥匙串）保存，界面只展示脱敏预览。

## 快速开始

### 环境要求

| 依赖 | 版本要求 |
| --- | --- |
| Python | 3.11 及以上（已在 3.14 验证） |
| Node.js | 18 及以上（已在 18.20 验证） |
| uv | 最新版即可 |

> **关于 Node 版本**：当前前端锁定 Vite 5 + React 18，在 Node 18 / 20 / 22 上均可运行。
> 注意 Node 18 已于 2025 年 4 月停止维护，建议尽早升级到 Node 20 或 22 LTS。

### 0. 用 `cw` 一键管理（推荐）

仓库根目录提供了一个交互式服务管理工具，免去手动切换目录、记三套命令：

```bash
./cw
```

方向键选择服务，支持前台运行（Ctrl+C 停止）、后台运行（日志写入 `.cw/logs/`）、
查看状态、停止服务、全部启动。首次运行会自动准备一次 CLI 环境（需要联网）。
详见 [cli/README.md](cli/README.md)。

> 前提是各服务的依赖已经装好（`backend` 的 `uv sync`、`frontend`/`desktop` 的
> `npm install`）。`cw` 只做启动与进程管理，不自动安装依赖，缺失时会给出提示。

下面的各节是手动启动方式，供需要单独调试某个服务时使用。

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

### 3. 智能镜头分割（可选）

Web 工作台的「智能镜头分割」页调用仓库根目录的 `vct` 命令行工具
（`vct` + `vctl/` 已作为本仓库的一部分维护，克隆仓库即可获得），
把多镜头素材按画面跳变切成单镜头片段。需要本机已安装
`scenedetect` 与 `ffmpeg`（页面顶部的环境自检会告诉你缺什么、怎么装）。

一次勾选多条视频时，**每一条都有自己的进度**：正在切的那一行显示「检测中」
或「切割中 3/38」，其余行留空。切完的片段点一下封面就能在页面里播放（支持
拖动进度条），弹窗底部可以「上一个 / 下一个」连着看。检测阶段刻意不给百分比
（有几个镜头得整条素材过完才知道）。

历史任务列表的「查看」是两级下钻：先看这条任务切了哪些视频，再点进某一条
看它自己的片段 —— 一次切十几条时，几百个片段混在一个网格里谁也认不出谁。
翻历史用的是弹窗，**不会把页面上正在跑的那条任务顶掉**。

以上原委见 `docs/镜头分割设计说明.md` 第 8、9、10 节。

### 素材放哪儿：`materials/`

仓库根目录的 `materials/` 是素材仓库，**按流程分成四段**，每一段只放一类东西。
规划的唯一权威来源是 `backend/app/core/materials.py`：

```
materials/
├── source/     ① 原始素材：待切的视频往这里拷（页面的默认输入目录）
├── clips/      ② 镜头分割产物：每条原片一个 <视频名>_scenes/（页面的默认输出目录）
├── subtitle/   ③ 字幕与文案产物（.srt / .txt），留给 vct 的字幕流程
└── output/     ④ 成片，待发布
```

| 分段 | 放什么 | 谁写进来的 |
| --- | --- | --- |
| `source/` | 相机、手机、剪辑软件导出的原片 | **你自己拷**。页面打开时输入目录默认停在这里 |
| `clips/` | 切好的单镜头片段 | 后端。每条原片单独建 `<视频名>_scenes/`，重名追加 `-2`、`-3` |
| `subtitle/` | 识别出的字幕、写好的文案 | 留给 vct 的字幕流程 |
| `output/` | 拼好待发布的成片 | 后续的合成 / 导出流程 |

根目录本身不放东西 —— 原片进 `source/`，产物进各自的分段目录，
「哪些是素材、哪些是产物」一眼可辨，也不会几百个文件糊在一层。

**入库的只有骨架**（`.gitignore` 里先 `materials/**` 全挡，再逐个放行）：

- 入库：`materials/README.md`（下面那份说明）与四个分段的 `.gitkeep` 占位文件
  —— git 不跟踪空目录，没有占位文件新克隆下来就只剩一个空文件夹；
- 不入库：里面的一切。原片动辄几百 MB 到几 GB，切分产物更是成倍增长，
  提交上去仓库就没法用了。

**目录骨架不用手动建**：后端每次启动都会按规划补出来
（`ensure_materials_layout()`，建不出来会退回用户主目录并记日志），
删了、或者把 `SCENE_MATERIALS_DIR` 指到别处，下次启动都是齐齐的四段。

> 第一次打开时 `source/` 必然是空的，页面会直接提示「往哪儿放」；
> 拷完素材点一下「重新扫描」就能刷出来，不用重开页面。

相关环境变量（均有默认值，见 `backend/.env.example`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SCENE_VCT_PATH` | 自动指向本仓库根目录的 `vct` | vct 可执行文件路径 |
| `SCENE_MATERIALS_DIR` | 仓库根目录的 `materials/` | 素材目录**根**，下有 source / clips / subtitle / output 四段（不存在时自动创建） |
| `SCENE_WORKER_ENABLED` | `true` | 是否启用后台切割工作线程 |
| `SCENE_JOB_VIDEO_TIMEOUT_SECONDS` | `3600` | 单条视频的硬超时 |
| `SCENE_INPUT_EXTENSIONS` | `.mp4,.mov,.mkv,.avi,.webm,.m4v` | 可处理的视频扩展名 |
| `SCENE_MAX_BATCH_FILES` | `200` | 单个任务最多处理的视频数 |

内置模板（参数定义在 `backend/app/core/scene_templates.py`，前端只展示不维护）：

| 模板 | 检测器 | 阈值 | 最短镜头 | 同一条素材的实测镜头数 |
| --- | --- | --- | --- | --- |
| 标准切分（默认） | adaptive | 默认(3.0) | 0.6s | 33 |
| 快速粗切 | content | 35 | 2.0s | 14 |
| 精细切分 | content | 20 | 0.4s | 38 |
| 抗运镜 | adaptive | 5.0 | 1.0s | 25 |
| 淡入淡出 | threshold | 默认(12.0) | 0.6s | 1 |

> 实测素材为 `裁剪后-9月16日-05.mp4`（带货口播，约 2 分钟）。
> **「淡入淡出」只检出 1 个镜头不是参数错误**：threshold 检测器只认黑场渐变，
> 普通硬切素材本来就没有黑场，这个模板只对特定片源有用。
> 拿不准就用「预览切点」先看效果 —— 预览不写任何文件。

两个开发期注意事项：

- 后端以 `--reload` 运行时，**修改后端文件会触发重启并中断正在执行的切割任务**
  （该任务会被标记为失败），这是 uvicorn 热重载的固有行为；
- `/api/v1/fs/list` 是面向本机单用户的无鉴权目录列举接口，
  **把 `HOST` 改成 `0.0.0.0` 暴露到局域网之前，必须先加鉴权或恢复根目录白名单**。

### 4. 启动桌面客户端（可选）

只有需要执行代理发布时才需要启动，后端与 Web 工作台可以先独立使用。

```bash
cd desktop

# 安装依赖
npm install

# 启动客户端
npm run dev
```

首次启动后到「设置」页点一次「保存并测试连接」，确认能连上后端。

## 运行测试

```bash
cd backend
uv run pytest
```

测试使用独立的内存数据库，不会触碰开发环境的数据文件。

客户端的自检（验证预加载桥、凭证加解密、配置读写）：

```bash
cd desktop
npm run build && SMOKE_TEST=1 npx electron . --user-data-dir=/tmp/cw-smoke
```

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
| DELETE | `/api/v1/contents/{id}` | 删除内容（被未完成任务引用时拒绝） |
| GET | `/api/v1/accounts` | 查询账号列表，支持平台、状态、关键字过滤 |
| POST | `/api/v1/accounts` | 创建账号（**不含凭证字段**） |
| GET | `/api/v1/accounts/{id}` | 获取账号详情 |
| PUT | `/api/v1/accounts/{id}` | 更新账号 |
| DELETE | `/api/v1/accounts/{id}` | 删除账号（存在任务记录时拒绝） |
| GET | `/api/v1/publish-tasks` | 分页查询发布任务 |
| POST | `/api/v1/publish-tasks` | 创建单个发布任务 |
| POST | `/api/v1/publish-tasks/batch` | 批量创建（一条内容分发到多个账号） |
| GET | `/api/v1/publish-tasks/statistics` | 发布任务统计 |
| GET | `/api/v1/publish-tasks/{id}` | 获取任务详情 |
| DELETE | `/api/v1/publish-tasks/{id}` | 删除任务（仅限已结束的任务） |
| POST | `/api/v1/publish-tasks/claim` | **客户端**认领待执行任务 |
| POST | `/api/v1/publish-tasks/{id}/report` | **客户端**上报发布结果 |
| POST | `/api/v1/publish-tasks/{id}/cancel` | 取消任务 |
| POST | `/api/v1/publish-tasks/{id}/retry` | 失败 / 已取消的任务重新入队 |

### 发布任务状态流转

```
pending ──认领──> running ──成功──> success
   │                 │
   │                 └──失败──> 未超重试上限 → 退回 pending
   │                           超过上限     → failed
   │
   └──取消──> cancelled        failed / cancelled ──重试──> pending
```

终态任务不再自动流转，只能通过 `retry` 显式重置回队列。

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
- 在 `desktop/src/main/publishers/` 中接入真实平台适配器（抖音、小红书等），替换当前的模拟发布器
- 为桌面客户端配置代码签名与公证，分发安装包
