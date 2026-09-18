# content-creation-workbench

## 仓库结构

| 目录 | 内容 |
| --- | --- |
| `backend/` | FastAPI 后端：内容管理、镜头分割、字幕提取、混剪 |
| `frontend/` | React 18 + antd 6 + Vite 前端（`src/pages` 页面、`src/hooks` 自定义 hook） |
| `cli/` `cw` `cw.cmd` | 命令行入口（Windows 下直接跑控制台脚本需显式 `PYTHONPATH` 兜底） |
| `desktop/` `vct/` | 桌面端 / 其它工具 |
| `docs/` | 各功能的设计说明（需求、流程、产物结构） |

## 前端约定（重要）

### 1. 页面只做编排

`frontend/src/pages/*.tsx` 里只允许两件事：**调 hook** 和 **拼 JSX**。
请求、轮询、状态机、错误提示这类逻辑一律不在页面里写 —— 页面里出现成片的
`useState` + `useEffect` + `fetch` 就是走偏了。展示用的纯函数（列定义、格式化调用、
局部渲染片段）留在页面里没关系，它们不持有状态。

### 2. 逻辑放哪儿：公用才进 `src/hooks/`

| 情况 | 放哪 |
| --- | --- |
| **两个及以上页面**用到的 hook | `frontend/src/hooks/`，并在 `frontend/src/hooks/index.ts` 里导出 |
| **只有一个页面**用到的 hook | 放这个页面自己的文件夹：`frontend/src/pages/<页面名>/useXxx.ts`，页面本体挪进同一文件夹的 `index.tsx`（路由 import 不用改，`./pages/MixCut` 会解析到目录的 `index.tsx`） |

判断标准就一条：**有没有第二个页面要用**。只有一个页面用的实现，不许塞进
`src/hooks/` —— 那里是公用仓库，混进私有实现后别人会以为它能复用、改动它会影响
到不相干的页面。

`frontend/src/hooks/` 现有（都是多页共用的）：

- `useApiMessage` —— 接口失败提示，`fail(error, '创建任务失败')` 取代满地的 `instanceof ApiError` 三元式；渲染它返回的 `contextHolder`
- `useAsyncData` —— 进页面拉一份数据（环境自检 / 模板 / 素材库），失败提示与 `onLoaded` 回填交给调用方
- `useSourceDir` —— 素材目录扫描 + 勾选（镜头分割 / 字幕提取）
- `useDirectoryPicker` —— 目录选择弹窗的开关（`open(key)` / `close()` / `active`）
- `useJobList` —— 历史任务列表（分页、静默失败）
- `useJobPolling` —— 单条任务的轮询（`useJobRunner` 内部用它；弹窗里单独盯一条任务时也用）
- `useJobRunner` —— 当前任务：创建 / 轮询 / 取消 / 删除 / 从历史点开

### 3. 跨页面一致的 UI 块放 `src/components/`

`SourceDirCard`、`JobProgressCard`、`HistoryCard`、`VideoPreviewModal`、
`jobColumns`（历史表列工厂）、`DirectoryPicker`。列定义、卡片外壳这类「三个页面逐字
一样」的 JSX 别再抄进页面。

### 4. 工具函数与类型

- 格式化（时间 / 时长 / 字节）只在 `frontend/src/utils/format.ts`：`formatDateTime`、
  `formatDuration`（null 或非正数给 `--:--`）、`formatElapsed`（已耗时）、`formatBytes`。
  **不要在 `src/types/*.ts` 里放工具函数** —— `types/scene.ts` 与 `types/subtitle.ts`
  曾各有一份 `formatBytes`，改一处要改三处。
- `src/types/*.ts` 只放类型与状态常量（`JOB_STATUS_META`、`isTerminalStatus` 这类
  与类型强相关的常量函数可以留）。
- 接口错误 → 文案统一走 `src/api/client.ts` 的 `describeError(error, fallback)`。

### 5. 轮询与回调的固定写法

- 回调进 ref（「最新 ref」模式）：`const latest = useRef({...}); useEffect(() => { latest.current = {...} })`，
  这样对外暴露的函数引用恒定，页面不必把它们写进依赖数组。
- 轮询 timer 的依赖只写 `jobId` 和「是否在跑」，不要以整个任务对象为依赖 —— 否则
  每次拿到新进度都重建 timer。
- 后端任务是异步跑的（前端 fetch 超时 15s），进度一律靠轮询，间隔取
  `useJobPolling` 导出的 `POLL_INTERVAL_MS`。

### 6. antd 6 注意点

- `message` 必须 `message.useMessage()` 取实例并渲染返回的 `contextHolder`
  （项目里没有 `<App>` 包裹，用静态方法会丢主题与语言）。
- 弹窗要卸载内容用 `destroyOnHidden`；`Table` 保持 `rowKey="id"`。
- 不额外套 `size="small"` 之类的尺寸微调，除非原设计如此。
- 页面不自己加外层 padding，留白由 `components/Layout.tsx` 的 Content 统一给。

### 7. 改完前端必须过这两关

```bash
cd frontend && npx tsc --noEmit   # strict + noUnusedLocals/noUnusedParameters，未用到的导入会报错
cd frontend && npm run build
```

新增 / 改造页面的完整清单见技能 `frontend-page`（`.claude/skills/frontend-page/SKILL.md`）。
