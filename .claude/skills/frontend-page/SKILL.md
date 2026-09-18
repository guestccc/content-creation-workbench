---
name: frontend-page
description: 新增或改造 frontend/ 下的页面时使用。保证页面只做编排、逻辑进自定义 hook，并决定 hook 该放 src/hooks/（多页共用）还是页面自己的文件夹（单页私有）。当用户说「加个页面」「这个页面逻辑太重」「把这段逻辑抽出去」「hook 放哪」时先读这份。
---

# 前端页面：只做编排，逻辑进 hook

## 动手之前：先查已有资产（不许直接开写）

```bash
cd frontend && sed -n '1,40p' src/hooks/index.ts   # 已有哪些 hook、各自负责什么
ls src/components                                    # 已有哪些跨页面 UI 块
sed -n '1,60p' src/utils/format.ts                   # 格式化函数
```

现有可复用清单（截至 2026-09）：

| 你要做的事 | 直接用它 |
| --- | --- |
| 接口失败提示 | `useApiMessage()` → `fail(error, '兜底文案')`，页面渲染 `contextHolder` |
| 进页面拉一份数据 | `useAsyncData({ load, failMessage, silent?, fail, onLoaded })` |
| 素材目录扫描 + 勾选 | `useSourceDir(fail)` + `<SourceDirCard>` |
| 目录选择弹窗 | `useDirectoryPicker<'input' \| 'output' \| ...>()` + `<DirectoryPicker>` |
| 历史任务列表（分页） | `useJobList<J>({ fetchList })` + `<HistoryCard>` + `jobStatusColumn` |
| 当前任务：创建/轮询/取消/删除 | `useJobRunner<J, P>({...})` |
| 单独盯一条任务 | `useJobPolling({ job, fetchJob, isTerminal, onUpdate, onFinish })` |
| 任务进度卡片 | `<JobProgressCard>`（头部 `JobTitle`、`stats`、`current` 提示） |
| 播放视频的弹窗 | `<VideoPreviewModal src videoKey caption />` |
| 时长 / 字节 / 时间格式化 | `src/utils/format.ts` |

找不到合适的再自己写，但**先按下面的位置规则决定放哪**。

## 位置规则（用户明确要求）

1. **两个及以上页面会用到** → `frontend/src/hooks/useXxx.ts`，并在 `src/hooks/index.ts`
   加导出和一行 docblock。
2. **只有一个页面会用到** → 放这个页面自己的文件夹，**不进 `src/hooks/`**：
   ```
   src/pages/MixCut/index.tsx        # 页面本体（原来是 MixCut.tsx，用 git mv 挪进来）
   src/pages/MixCut/useMixSections.ts   # 只有这个页面用的 hook
   ```
   路由 `src/App.tsx` 的 `import MixCut from './pages/MixCut'` 不用改，会解析到目录的 `index.tsx`。
3. 拿不准时按「第二个页面会不会用」判断；宁可先放页面文件夹，等真的出现第二个页面
   再 `git mv` 进 `src/hooks/` —— 反向搬（从公用仓库挪回页面）成本更高，而且**放进
   `src/hooks/` 的私有实现会诱使后来的人去改它**。

## 新页面骨架

```tsx
// src/pages/Xxx.tsx（或 src/pages/Xxx/index.tsx，需要私有 hook 时）
export default function Xxx() {
  // 1. 提示 + 弹窗开关
  const { message, fail, contextHolder } = useApiMessage()
  const picker = useDirectoryPicker<'input' | 'output'>()

  // 2. 进页面要拉的数据（onLoaded 里回填默认值，别写额外 effect）
  const environment = useAsyncData({
    load: () => fetchXxxEnvironment(),
    failMessage: '环境自检失败',
    silent: true,                     // 自检失败不阻断页面时
    onLoaded: (data) => setInputDir((c) => c || data.default_input_dir),
  })

  // 3. 历史 + 当前任务
  const history = useJobList<XxxJob>({ fetchList: fetchXxxJobs })
  const runner = useJobRunner<XxxJob, XxxPayload>({
    create: createXxxJob, cancel: cancelXxxJob, remove: deleteXxxJob,
    fetchJob: fetchXxxJob, isTerminal: (j) => isTerminalStatus(j.status), fail,
    onChanged: history.reload,        // 任务增删改后刷新历史
    onRemoved: () => setDetail(null), // 删掉页面上正看的那条时清残留
  })

  // 4. 只剩下的本地状态：纯展示用的开关（哪个弹窗开着、预览哪条）
  // 5. return 里只拼布局
}
```

## 页面里允许留下什么

- 调 hook、拼布局；
- 纯展示的局部渲染函数 / 列定义 / 小工具（不持有状态，如 `splitOrder`、`ClipThumb`）；
- 一次性的 UI 开关（`previewClip`、`orderDetail` 这类）。

**不允许**：`useEffect` 里发请求、手写 `setInterval` 轮询、`error instanceof ApiError`
三元式、把某个接口的调用顺序/状态机写在页面里。

## 收尾自检

```bash
cd frontend && npx tsc --noEmit   # strict；未用到的 import/参数会直接报错
cd frontend && npm run build
```

- [ ] 页面里没有 `useEffect` + `fetch` 形态的请求逻辑
- [ ] 没有第二份 `instanceof ApiError`
- [ ] 新增 hook 的位置符合上面的「位置规则」
- [ ] `src/hooks/index.ts` 里补了导出与一行说明（仅当 hook 确实是公用的）
- [ ] 未使用的 import 已删（`noUnusedLocals` 开着）
- [ ] 没有顺手改掉原有的视觉与文案（尺寸、`Tag` 颜色、提示语要与改造前一致）
