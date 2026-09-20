import { useCallback, useEffect, useState } from 'react'

import { call } from './api'
import AccountsPage from './pages/Accounts'
import PublishNowPage from './pages/PublishNow'
import PublishTasksPage from './pages/PublishTasks'
import SettingsPage from './pages/Settings'

/** 页面标识 */
type TabKey = 'publish' | 'tasks' | 'accounts' | 'settings'

/** 侧边栏导航项 */
const NAV_ITEMS: Array<{ key: TabKey; label: string; desc: string }> = [
  { key: 'publish', label: '一键分发', desc: '选择内容与账号，批量创建发布任务' },
  { key: 'tasks', label: '发布任务', desc: '查看任务队列，控制执行与重试' },
  { key: 'accounts', label: '账号与凭证', desc: '维护平台账号，管理本机加密凭证' },
  { key: 'settings', label: '设置', desc: '后端地址、轮询策略与发布器状态' }
]

/** 待处理任务数量的刷新间隔（毫秒） */
const BADGE_REFRESH_MS = 5_000

export default function App(): JSX.Element {
  const [tab, setTab] = useState<TabKey>('publish')
  const [pendingCount, setPendingCount] = useState(0)

  /** 拉取待处理任务数，用于侧边栏角标 */
  const refreshBadge = useCallback(async () => {
    try {
      const stats = await call(window.api.tasks.statistics())
      setPendingCount((stats.by_status.pending ?? 0) + (stats.by_status.running ?? 0))
    } catch {
      // 后端未启动时静默忽略，具体错误由各页面自行提示
      setPendingCount(0)
    }
  }, [])

  useEffect(() => {
    void refreshBadge()
    const timer = setInterval(() => void refreshBadge(), BADGE_REFRESH_MS)
    return () => clearInterval(timer)
  }, [refreshBadge])

  const current = NAV_ITEMS.find((item) => item.key === tab) ?? NAV_ITEMS[0]

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-title">
          content-creation-workbench
          <span className="sidebar-subtitle">客户端 · 代理发布</span>
        </div>

        {NAV_ITEMS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={`nav-item${tab === item.key ? ' active' : ''}`}
            onClick={() => setTab(item.key)}
          >
            <span>{item.label}</span>
            {item.key === 'tasks' && pendingCount > 0 ? (
              <span className="nav-badge">{pendingCount}</span>
            ) : null}
          </button>
        ))}

        <div className="sidebar-footer">
          登录凭证仅加密保存在本机，
          <br />
          不会上传到服务端。
        </div>
      </aside>

      <main className="main">
        <header className="page-header">
          <div>
            <h1 className="page-title">{current.label}</h1>
            <p className="page-desc">{current.desc}</p>
          </div>
        </header>

        {tab === 'publish' ? <PublishNowPage onTaskCreated={refreshBadge} /> : null}
        {tab === 'tasks' ? <PublishTasksPage onChanged={refreshBadge} /> : null}
        {tab === 'accounts' ? <AccountsPage /> : null}
        {tab === 'settings' ? <SettingsPage /> : null}
      </main>
    </div>
  )
}
