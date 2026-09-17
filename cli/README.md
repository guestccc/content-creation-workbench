# cw — 内容创作工作台开发服务管理 CLI

一个交互式菜单，用来选择并管理三个开发服务（后端 / 前端 / 桌面端），
替代「记住三套命令、三个工作目录」的手工流程。

## 使用

在仓库根目录运行：

```bash
./cw
```

首次运行会自动执行一次 `uv sync --project cli` 准备环境（需要联网），
之后直接执行 `cli/.venv/bin/cw`，没有重复的环境校验开销。

> 依赖版本变更后 shim 不会自动更新，需要手动执行一次：
> `uv sync --project cli`

## 功能

- **主菜单即状态视图**：每个服务实时显示 运行中 / 未运行 / 端口被占 / 记录残留
- **前台运行**：输出直接打到当前终端，Ctrl+C 停止（整组退出，不留孤儿进程）
- **后台运行**：日志写入 `.cw/logs/`，可以随时回来停止或查看日志
- **全部启动**：按 后端 → 前端 → 桌面端 的顺序依次后台启动
- **启动前检查**：依赖缺失（venv / node_modules / npm）时给出明确的安装提示；
  后端端口与前端代理配置不一致时给出告警

## 它是怎么保证「不误杀」的

停止服务是向**整个进程组**发信号（`npm run dev` 和 `uvicorn --reload`
都是进程树，只杀顶层进程会留下占着端口的孤儿）。为了防止 PID 复用
导致误杀无关进程，每次停止前做三重身份校验：

1. pidfile 里的 `create_time` 指纹与当前进程一致（PID 被复用后必然不同）
2. 进程组 ID 与记录一致
3. 组长就是当初那个 PID 自己（`start_new_session` 的保证）

任何一条不满足都拒绝发信号。

## 运行时数据

`.cw/` 目录（已被 gitignore）：

```
.cw/
├── pids/<service>.json   # 进程身份记录（含 create_time 指纹）
└── logs/<service>.log    # 后台模式的 stdout/stderr
```

## 测试

```bash
uv run --project cli pytest          # 单元测试（离线）
python3 cli/scripts/e2e_pty.py       # 端到端：后台启动→健康检查→停止→验证清理
python3 cli/scripts/e2e_foreground.py # 端到端：前台 Ctrl+C 整组退出的关键性质
```

端到端脚本用伪终端驱动真实菜单，需要本机已装好后端依赖（`cd backend && uv sync`）。
