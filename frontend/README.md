# 内容创作工作台 · 前端应用

基于 React + TypeScript + Vite 的内容创作工作台前端。

## 技术选型

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| React | 18.3 | UI 框架 |
| TypeScript | 5.6 | 类型系统（严格模式） |
| Vite | 5.4 | 构建工具与开发服务器 |
| React Router | 6.x | 路由 |
| antd | 6.x | **唯一的 UI 组件库**，全站界面组件均来自它 |

> **关于 antd 6**：v6 的组件样式是运行时按 CSS 变量生成的，静态产物里 grep 不到
> `.ant-` 类名，属正常现象；调试样式请以浏览器运行时 DOM 为准。v6 把弹窗内容区的
> 类名改成了 `ant-modal-container`（v5 是 `ant-modal-content`），写自动化脚本时会遇到。

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
│   ├── contents.ts     # 内容相关接口
│   ├── filesystem.ts   # 目录列举（配合目录选择弹窗）
│   ├── mix.ts          # 智能混剪接口
│   └── scene.ts        # 镜头分割接口
├── components/
│   ├── Layout.tsx      # 全局布局（antd Layout + Menu 侧边导航）
│   ├── ContentFormModal.tsx  # 内容创建/编辑弹窗（antd Modal + Form）
│   └── DirectoryPicker.tsx   # 目录选择弹窗
├── pages/
│   ├── Dashboard.tsx   # 工作台概览
│   ├── ContentList.tsx # 内容管理
│   ├── SceneSplit.tsx  # 智能镜头分割
│   └── MixCut.tsx      # 智能混剪
├── types/              # 类型定义与常量（content / scene / mix）
├── utils/format.ts     # 格式化工具
├── App.tsx             # 路由定义
├── main.tsx            # 应用入口（ConfigProvider 主题在这里）
└── index.css           # 页面底座与设计变量（不含组件样式）
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

## UI 与样式约定

**界面组件一律使用 antd，不手写原生表单 / 表格 / 弹窗 / 按钮。** antd 已经处理过
这些组件的边界情况（翻页越界、空数据、表单校验、键盘操作、焦点管理），自己写只会漏；
两套组件混用还会让人分不清「改哪儿才会生效」。

主题只有**一个来源** —— `main.tsx` 里 `ConfigProvider` 的 `theme.token`：

```tsx
<ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#2563eb', borderRadius: 10 } }}>
```

`src/index.css` 因此只剩两样东西，不再有 `.btn` / `.card` / `.table` 这类手写组件样式：

1. **设计变量**（`:root` 里的 `--color-*` / `--radius-*`）—— 与 `ConfigProvider` 的
   token 对齐，供少数需要跟主题一致的自定义元素（如状态色小色条）引用；
2. **页面底座** —— `html/body` 的高度、字体、背景色。

调整整体视觉时改这两处，而不是逐个页面覆盖组件样式。

### 两个容易踩的点

- **提示与确认框要用 hook 形式**：`message.useMessage()` / `Modal.useModal()`，
  不要用 `message.success()` 静态方法 —— 后者拿不到 `ConfigProvider` 的主题与语言。
- **antd 会在两个汉字之间插空格**：文案恰好是两个汉字时（如「刷新」「搜索」），
  渲染出的 DOM 文本是 `刷 新`。写自动化脚本定位按钮时用正则 `/刷\s*新/`。

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
