import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { fetchContents, fetchHealth, fetchStatistics } from '../api/contents'
import { ApiError } from '../api/client'
import type { Content, ContentStatistics, HealthData } from '../types/content'
import { STATUS_META, STATUS_ORDER } from '../types/content'
import { formatDateTime } from '../utils/format'

/**
 * 工作台概览页。
 *
 * 展示内容统计、后端服务状态与最近更新的内容。
 */
export default function Dashboard() {
  const [stats, setStats] = useState<ContentStatistics | null>(null)
  const [recent, setRecent] = useState<Content[]>([])
  const [health, setHealth] = useState<HealthData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')

    // 三个请求互不依赖，并发发起；用 allSettled 保证单个失败不影响其余数据展示
    const [statsResult, listResult, healthResult] = await Promise.allSettled([
      fetchStatistics(),
      fetchContents({ page: 1, page_size: 5 }),
      fetchHealth(),
    ])

    if (statsResult.status === 'fulfilled') {
      setStats(statsResult.value)
    }
    if (listResult.status === 'fulfilled') {
      setRecent(listResult.value.items)
    }
    if (healthResult.status === 'fulfilled') {
      setHealth(healthResult.value)
    }

    // 统计是首页核心数据，它失败时给出明确提示
    if (statsResult.status === 'rejected') {
      const reason: unknown = statsResult.reason
      setError(reason instanceof ApiError ? reason.message : '加载失败，请稍后重试')
    }

    setLoading(false)
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const databaseOk = health?.database === 'connected'
  const serviceOk = health?.status === 'ok'

  return (
    <>
      <header className="page-header">
        <div>
          <h2 className="page-header__title">工作台概览</h2>
          <p className="page-header__desc">查看内容创作的整体进展与最近动态</p>
        </div>
        <button type="button" className="btn" onClick={() => void load()} disabled={loading}>
          {loading ? '刷新中…' : '刷新'}
        </button>
      </header>

      {error && <div className="alert alert--error">⚠️ {error}</div>}

      {/* 顶部统计卡片：总数 + 各状态分布 */}
      <section className="stat-grid">
        <article className="stat-card">
          <div className="stat-card__label">内容总数</div>
          <div className="stat-card__value">{stats ? stats.total : '—'}</div>
          <div className="stat-card__accent" style={{ background: '#2563eb' }} />
        </article>

        {STATUS_ORDER.map((status) => (
          <article className="stat-card" key={status}>
            <div className="stat-card__label">{STATUS_META[status].label}</div>
            <div className="stat-card__value">{stats ? stats.by_status[status] : '—'}</div>
            <div className="stat-card__accent" style={{ background: STATUS_META[status].color }} />
          </article>
        ))}
      </section>

      {/* 后端服务状态 */}
      <section className="card" style={{ marginBottom: 22 }}>
        <div className="card__header">
          <h3 className="card__title">服务状态</h3>
          {health && (
            <span
              className="badge"
              style={{
                color: serviceOk ? '#166534' : '#92400e',
                background: serviceOk ? '#f0fdf4' : '#fffbeb',
              }}
            >
              {serviceOk ? '运行正常' : '降级运行'}
            </span>
          )}
        </div>
        <div className="card__body">
          {health ? (
            <div className="field-row">
              <div>
                <div className="text-muted" style={{ fontSize: 13 }}>
                  应用名称
                </div>
                <div>{health.app_name}</div>
              </div>
              <div>
                <div className="text-muted" style={{ fontSize: 13 }}>
                  版本
                </div>
                <div className="text-mono">{health.version}</div>
              </div>
              <div>
                <div className="text-muted" style={{ fontSize: 13 }}>
                  数据库
                </div>
                <div style={{ color: databaseOk ? 'var(--color-success)' : 'var(--color-danger)' }}>
                  {databaseOk ? '已连接' : '未连接'}
                </div>
              </div>
            </div>
          ) : (
            <div className="text-muted">
              无法获取服务状态，请确认后端服务已启动（默认 http://127.0.0.1:8000）
            </div>
          )}
        </div>
      </section>

      {/* 最近更新的内容 */}
      <section className="card">
        <div className="card__header">
          <h3 className="card__title">最近更新</h3>
          <Link to="/contents" className="btn btn--sm">
            查看全部
          </Link>
        </div>

        {loading ? (
          <div className="placeholder">加载中…</div>
        ) : recent.length === 0 ? (
          <div className="placeholder">
            还没有任何内容，去「内容管理」页创建第一条吧
          </div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>标题</th>
                <th>平台</th>
                <th>状态</th>
                <th>更新时间</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((item) => (
                <tr key={item.id}>
                  <td className="table__title">{item.title}</td>
                  <td>{item.platform || <span className="text-muted">—</span>}</td>
                  <td>
                    <span
                      className="badge"
                      style={{
                        color: STATUS_META[item.status].color,
                        background: `${STATUS_META[item.status].color}1a`,
                      }}
                    >
                      {STATUS_META[item.status].label}
                    </span>
                  </td>
                  <td className="text-muted">{formatDateTime(item.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  )
}
