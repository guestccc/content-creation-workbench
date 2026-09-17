# 内容创作工作台 · 前端应用

基于 React + TypeScript + Vite 的内容创作工作台前端。

## 技术选型

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| React | 18.3 | UI 框架 |
| TypeScript | 5.6 | 类型系统（严格模式） |
| Vite | 5.4 | 构建工具与开发服务器 |
| React Router | 6.x | 路由 |

> **版本说明**：该组合在 Node 18 / 20 / 22 上均可运行。
> 若已将 Node 升级到 20.19+ 或 22.12+，可平滑升级到 Vite 7。

## 快速开始

```bash
# 安装依赖
npm install

# 启动开发服务器（默认 http://127.0.0.1:5173）
npm run dev
```

启动前请确保后端已在 `http://127.0.0.1:8000` 运行。

## 可用命令

| 命令 | 说明 |
| --- | --- |
| `npm run dev` | 启动开发服务器 |
| `npm run build` | 类型检查 + 生产构建，产物在 `dist/` |
| `npm run preview` | 本地预览构建产物 |
| `npm run typecheck` | 仅执行类型检查 |

## 目录结构

```
frontend/src/
├── api/
│   ├── client.ts       # fetch 封装：超时、错误转换、响应拆包
│   └── contents.ts     # 内容相关接口
├── components/
│   ├── Layout.tsx      # 全局布局（侧边导航）
│   └── ContentFormModal.tsx  # 内容创建/编辑弹窗
├── pages/
│   ├── Dashboard.tsx   # 工作台概览
│   └── ContentList.tsx # 内容管理
├── types/content.ts    # 类型定义与常量
├── utils/format.ts     # 格式化工具
├── App.tsx             # 路由定义
├── main.tsx            # 应用入口
└── index.css           # 全局样式（CSS 变量主题）
```

## 接口调用方式

开发环境下，`vite.config.ts` 已把 `/api` 前缀的请求代理到后端：

```ts
// vite.config.ts
server: {
  proxy: {
    '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
  },
}
```

因此前端代码中统一使用**相对路径**，无需关心后端地址，也不存在跨域问题。

### 请求封装

`api/client.ts` 统一处理三件事：

1. **超时控制**：默认 15 秒，超时抛出 `ApiError('请求超时…', 'TIMEOUT', 0)`；
2. **响应拆包**：自动把 `{ success: true, data }` 拆出 `data` 返回；
3. **错误归一**：网络异常、业务错误统一转换为 `ApiError`，页面只需 `catch` 一种异常。

```ts
import { ApiError } from '../api/client'
import { fetchContents } from '../api/contents'

try {
  const data = await fetchContents({ page: 1, page_size: 10 })
  console.log(data.items)
} catch (err) {
  if (err instanceof ApiError) {
    console.error(err.code, err.message)   // 如 NOT_FOUND、VALIDATION_ERROR
    console.error(err.details)             // 字段级校验明细
  }
}
```

## 环境变量

只有以 `VITE_` 开头的变量会被注入前端代码，**切勿在其中放置任何密钥**。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `/api/v1` | 接口基础路径 |

如需覆盖，复制 `.env.example` 为 `.env.local` 后修改（该文件已被 git 忽略）。

## 样式约定

`src/index.css` 使用 CSS 变量集中管理主题色与圆角、阴影：

```css
:root {
  --color-primary: #2563eb;
  --color-text: #1f2328;
  --radius-md: 10px;
}
```

调整整体视觉时优先修改变量，而非逐个覆盖组件样式。

## 构建与部署

```bash
npm run build
```

产物输出到 `dist/`，可托管在任意静态服务器（Nginx、对象存储、CDN）。

部署到生产环境时需要注意：

- 前端为单页应用，需将**所有未匹配路径回退到 `index.html`**，否则刷新子路由会 404；
- 若前后端不同域，需通过 `VITE_API_BASE_URL` 指向后端地址，并在后端 `CORS_ORIGINS` 中加入前端域名。

## 后续可扩展方向

- 引入组件库（如 Ant Design）统一交互细节
- 引入富文本 / Markdown 编辑器提升创作体验
- 引入数据请求库（如 TanStack Query）处理缓存与重试
- 补充组件测试（Vitest + Testing Library）
