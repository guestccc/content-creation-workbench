# 内容创作工作台客户端（Electron）

桌面客户端，负责**代理发布**：从后端认领发布任务，在本机解密凭证、
调用平台适配器完成发布，再把结果回传。

后端只负责排队与状态流转，**登录凭证永远不出本机**。

## 快速开始

```bash
cd desktop
npm install

# 另开一个终端启动后端
cd ../backend && uv run uvicorn app.main:app --reload

npm run dev
```

首次启动后建议按这个顺序操作：

1. 「设置」页确认后端地址（默认 `http://127.0.0.1:8000`），点「保存并测试连接」；
2. 「账号与凭证」页添加平台账号，并为需要登录的账号保存凭证；
3. Web 工作台里创建内容；
4. 「一键分发」页选择内容与账号，创建发布任务；
5. 「发布任务」页点「启动执行」，观察队列实时执行。

## 功能

| 功能 | 说明 |
| --- | --- |
| 自动化发布流程 | 一键把一条内容分发到多个账号，客户端自动完成认领、发布、上报 |
| 发布任务队列 | 任务列表、状态筛选、实时运行日志、手动取消与重试 |
| 多账号凭证管理 | 账号元信息存后端，登录凭证经系统加密保存在本机 |

## 安全设计

这几条是硬约束，改动代码时不要放宽：

1. **凭证不出本机**：凭证只以 `safeStorage` 加密后的密文形式存放在用户数据目录，
   从不经过后端接口，也从不写入数据库；
2. **不做明文降级**：系统加密能力不可用时直接拒绝保存，不会退回明文存储；
3. **界面只拿脱敏值**：主进程从不把凭证明文交给渲染进程，界面展示的是 `abcd****wxyz`；
4. **渲染进程无 Node 权限**：`contextIsolation: true`、`nodeIntegration: false`、`sandbox: true`，
   渲染进程只能通过预加载脚本暴露的语义化方法与主进程通信；
5. **网络请求集中在主进程**：后端地址是单一配置项，渲染进程不直接联网；
6. **不给系统权限**：所有权限申请一律拒绝，站外跳转交给系统浏览器。

凭证文件位于：

- macOS：`~/Library/Application Support/content-workbench-desktop/credentials.json`
- Windows：`%APPDATA%/content-workbench-desktop/credentials.json`

文件里只有密文。密钥由系统钥匙串托管，**换机器或清除钥匙串条目后无法解密，需要重新保存**。

## 目录结构

```
src/
├── main/                     # 主进程
│   ├── index.ts              # 入口：窗口、安全策略、自检
│   ├── ipc/                  # IPC 处理器（统一返回 { ok, data | error }）
│   ├── publishers/           # 平台发布器适配器
│   │   ├── types.ts          # Publisher 接口定义
│   │   ├── mock.ts           # 模拟发布器（当前默认实现）
│   │   └── registry.ts       # 平台 → 适配器解析
│   └── services/
│       ├── app-config.ts     # 本地配置读写
│       ├── api-client.ts     # 后端接口封装
│       ├── credential-store.ts  # 凭证加解密与落盘
│       └── scheduler.ts      # 发布任务调度器
├── preload/index.ts          # contextBridge 桥接
├── renderer/src/             # 渲染进程（React）
│   ├── pages/                # 一键分发 / 发布任务 / 账号与凭证 / 设置
│   └── api.ts                # IPC 结果拆包与统一错误类型
└── shared/                   # 三端共用的类型与常量
```

## 接入真实平台

目前所有平台都走模拟发布器。接入真实平台只需要两步：

1. 在 `src/main/publishers/` 下新建适配器，实现 `Publisher` 接口：

```ts
export class DouyinPublisher implements Publisher {
  readonly id = 'douyin'
  readonly displayName = '抖音'
  readonly platforms = ['抖音'] as const
  readonly requiresCredential = true
  readonly isPlaceholder = false

  async publish(request: PublishRequest, context: PublishContext): Promise<PublishResult> {
    // 用 request.credential 里的登录态调用平台接口
    // 注意：不要把 credential 写进日志或持久化
    return { url: 'https://...' }
  }
}
```

2. 在 `registry.ts` 的 `REAL_PUBLISHERS` 数组中注册。

调度器、IPC、界面都不需要改动。

## 命令

| 命令 | 作用 |
| --- | --- |
| `npm run dev` | 开发模式，带热更新 |
| `npm run build` | 类型检查 + 构建到 `out/` |
| `npm run typecheck` | 主进程与渲染进程分别做类型检查 |
| `npm run pack:dir` | 打包成免安装目录，便于本地验证 |
| `npm run dist:mac` | 打包 macOS 安装包（dmg） |
| `npm run dist:win` | 打包 Windows 安装包（nsis） |

## 自检

改动主进程或安全配置后，跑一次自检：

```bash
npm run build && SMOKE_TEST=1 npx electron . --user-data-dir=/tmp/cw-smoke
```

它会验证三件事并以退出码反映结果：

- `sandbox + contextIsolation` 下预加载桥是否注入成功（失败表现为整页白屏）；
- 凭证加解密往返是否一致、摘要是否脱敏；
- 配置文件读写是否正常。

配合 `--user-data-dir` 使用可避免动到真实的应用数据。

## 环境要求

- Node.js 18 及以上
- 已启动的后端服务（见 `../backend/README.md`）
