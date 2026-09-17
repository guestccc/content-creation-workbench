/** 发布任务队列：查看任务、控制调度器、处理失败任务。 */

import { useCallback, useEffect, useState } from 'react'

import { TASK_STATUS_META, TASK_STATUS_ORDER } from '@shared/constants'
import type { PublishTask, PublishTaskStatistics, PublishTaskStatus, SchedulerEvent, SchedulerStatus } from '@shared/types'

import { call, toMessage } from '../api'
import StatusBadge from '../components/StatusBadge'
import { formatDateTime, truncate } from '../utils/format'

interface Props {
  /** 任务状态发生变化时通知外层刷新角标 */
  onChanged: () => void
}

/** 列表自动刷新间隔（毫秒） */
const LIST_REFRESH_MS = 4_000

/** 运行日志最多保留的条数 */
const MAX_LOG_ITEMS = 100

export default function PublishTasksPage({ onChanged }: Props): JSX.Element {
  const [tasks, setTasks] = useState<PublishTask[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [statusFilter, setStatusFilter] = useState<PublishTaskStatus | ''>('')
  const [stats, setStats] = useState<PublishTaskStatistics | null>(null)
  const [schedulerStatus, setSchedulerStatus] = useState<SchedulerStatus | null>(null)
  const [logs, setLogs] = useState<SchedulerEvent[]>([])

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busyTaskId, setBusyTaskId] = useState<number | null>(null)
  const [notice, setNotice] = useState<{ kind: 'success' | 'error'; text: string } | null>(null)

  const pageSize = 20

  /** 拉取列表、统计与调度器状态 */
  const refresh = useCallback(async () => {
    try {
      const [listData, statistics, status] = await Promise.all([
        call(
          window.api.tasks.list({
            page,
            page_size: pageSize,
            status: statusFilter || undefined
          })
        ),
        call(window.api.tasks.statistics()),
        call(window.api.scheduler.status())
      ])
      setTasks(listData.items)
      setTotal(listData.total)
      setStats(statistics)
      setSchedulerStatus(status)
      setError('')
    } catch (err) {
      setError(toMessage(err))
    } finally {
      setLoading(false)
    }
  }, [page, statusFilter])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // 队列执行中时定时刷新，让状态变化能及时反映到界面上
  useEffect(() => {
    const timer = setInterval(() => void refresh(), LIST_REFRESH_MS)
    return () => clearInterval(timer)
  }, [refresh])

  // 订阅调度器推送的运行日志
  useEffect(() => {
    const unsubscribe = window.api.scheduler.onEvent((event) => {
      setLogs((prev) => [event, ...prev].slice(0, MAX_LOG_ITEMS))
      // 任务状态有变化时才刷新列表，避免无谓请求
      if (event.type !== 'poll') {
        void refresh()
        onChanged()
      }
    })
    return unsubscribe
  }, [refresh, onChanged])

  /** 启停调度器 */
  async function toggleScheduler(): Promise<void> {
    if (!schedulerStatus) {
      return
    }
    try {
      const next = schedulerStatus.running
        ? await call(window.api.scheduler.stop())
        : await call(window.api.scheduler.start())
      setSchedulerStatus(next)
      // 停止时把日志置顶一条提示，方便确认操作已生效
      setLogs((prev) =>
        [
          {
            type: next.running ? 'started' : 'stopped',
            at: new Date().toISOString(),
            message: next.running ? '已启动发布队列' : '已停止发布队列'
          } as SchedulerEvent,
          ...prev
        ].slice(0, MAX_LOG_ITEMS)
      )
    } catch (err) {
      setNotice({ kind: 'error', text: toMessage(err) })
    }
  }

  /** 对单条任务执行动作 */
  async function runAction(
    taskId: number,
    action: 'cancel' | 'retry' | 'remove',
    successText: string
  ): Promise<void> {
    setBusyTaskId(taskId)
    setNotice(null)
    try {
      if (action === 'cancel') {
        await call(window.api.tasks.cancel(taskId))
      } else if (action === 'retry') {
        await call(window.api.tasks.retry(taskId))
      } else {
        await call(window.api.tasks.remove(taskId))
      }
      setNotice({ kind: 'success', text: successText })
      await refresh()
      onChanged()
    } catch (err) {
      setNotice({ kind: 'error', text: toMessage(err) })
    } finally {
      setBusyTaskId(null)
    }
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const activeCount = schedulerStatus?.activeTaskIds.length ?? 0

  return (
    <>
      {error ? (
        <div className="alert alert-error">
          加载失败：{error}
          <br />
          请确认后端服务已启动，并在「设置」页检查服务地址。
        </div>
      ) : null}

      {notice ? (
        <div className={`alert alert-${notice.kind === 'success' ? 'success' : 'error'}`}>
          {notice.text}
        </div>
      ) : null}

      <div className="card">
        <div className="toolbar" style={{ marginBottom: 12 }}>
          <h2 className="card-title" style={{ margin: 0 }}>
            执行器
          </h2>
          <StatusBadge
            label={schedulerStatus?.running ? '运行中' : '已停止'}
            color={schedulerStatus?.running ? '#16a34a' : '#64748b'}
          />
          <div className="spacer" />
          <span className="text-sm text-muted">
            正在执行 {activeCount} 条 · 本次成功 {schedulerStatus?.successCount ?? 0} 条 · 失败{' '}
            {schedulerStatus?.failedCount ?? 0} 条
          </span>
          <button
            type="button"
            className={schedulerStatus?.running ? 'btn btn-danger' : 'btn btn-primary'}
            onClick={() => void toggleScheduler()}
          >
            {schedulerStatus?.running ? '停止执行' : '启动执行'}
          </button>
        </div>

        <p className="card-desc" style={{ marginBottom: 12 }}>
          执行器会按设置的间隔向后端认领待发布任务，在本机解密凭证并调用平台适配器发布，
          完成后把结果回传。停止后正在执行的任务会跑完，其余任务留待下次认领。
        </p>

        {schedulerStatus?.lastError ? (
          <div className="alert alert-warning">最近一次错误：{schedulerStatus.lastError}</div>
        ) : null}

        <div className="log-list">
          {logs.length === 0 ? (
            <div className="empty" style={{ padding: '20px' }}>
              暂无运行日志
            </div>
          ) : (
            logs.map((log, index) => (
              <div key={`${log.at}-${index}`} className="log-item">
                <span className="log-time">{log.at.slice(11, 19)}</span>
                <span
                  className={
                    log.type === 'error' || log.type === 'task-failed'
                      ? 'log-error'
                      : log.type === 'task-succeeded'
                        ? 'log-success'
                        : ''
                  }
                >
                  {log.message}
                </span>
              </div>
            ))
          )}
        </div>
      </div>

      {stats ? (
        <div className="stat-row">
          <div className="stat-card">
            <div className="stat-value">{stats.total}</div>
            <div className="stat-label">任务总数</div>
          </div>
          {TASK_STATUS_ORDER.map((status) => (
            <div className="stat-card" key={status}>
              <div className="stat-value" style={{ color: TASK_STATUS_META[status].color }}>
                {stats.by_status[status] ?? 0}
              </div>
              <div className="stat-label">{TASK_STATUS_META[status].label}</div>
            </div>
          ))}
        </div>
      ) : null}

      <div className="toolbar">
        <label className="field-label" htmlFor="status-filter">
          状态筛选
        </label>
        <select
          id="status-filter"
          style={{ width: 160 }}
          value={statusFilter}
          onChange={(event) => {
            setStatusFilter(event.target.value as PublishTaskStatus | '')
            setPage(1)
          }}
        >
          <option value="">全部</option>
          {TASK_STATUS_ORDER.map((status) => (
            <option key={status} value={status}>
              {TASK_STATUS_META[status].label}
            </option>
          ))}
        </select>
        <div className="spacer" />
        <button type="button" className="btn btn-sm" onClick={() => void refresh()}>
          刷新
        </button>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th style={{ width: 60 }}>ID</th>
              <th>内容</th>
              <th>账号</th>
              <th style={{ width: 90 }}>状态</th>
              <th style={{ width: 80 }}>重试</th>
              <th style={{ width: 160 }}>计划时间</th>
              <th>结果 / 失败原因</th>
              <th style={{ width: 180 }}>操作</th>
            </tr>
          </thead>
          <tbody>
            {loading && tasks.length === 0 ? (
              <tr>
                <td colSpan={8}>
                  <div className="empty">正在加载…</div>
                </td>
              </tr>
            ) : tasks.length === 0 ? (
              <tr>
                <td colSpan={8}>
                  <div className="empty">暂无发布任务，到「一键分发」页创建。</div>
                </td>
              </tr>
            ) : (
              tasks.map((task) => {
                const meta = TASK_STATUS_META[task.status]
                const busy = busyTaskId === task.id
                const canCancel = task.status === 'pending' || task.status === 'running'
                const canRetry = task.status === 'failed' || task.status === 'cancelled'
                const canDelete =
                  task.status === 'success' ||
                  task.status === 'failed' ||
                  task.status === 'cancelled'

                return (
                  <tr key={task.id}>
                    <td className="mono">{task.id}</td>
                    <td title={task.content_title}>
                      #{task.content_id} {truncate(task.content_title, 22)}
                    </td>
                    <td>
                      {task.account_nickname}
                      <div className="text-muted text-sm">{task.platform}</div>
                    </td>
                    <td>
                      <StatusBadge label={meta.label} color={meta.color} />
                    </td>
                    <td className="text-sm text-muted">
                      {task.retry_count} / {task.max_retries}
                    </td>
                    <td className="text-sm text-muted">
                      {task.scheduled_at ? formatDateTime(task.scheduled_at) : '立即执行'}
                    </td>
                    <td className="text-sm">
                      {task.status === 'success' && task.result_url ? (
                        <button
                          type="button"
                          className="link"
                          onClick={() => void window.api.system.openExternal(task.result_url)}
                        >
                          查看发布结果
                        </button>
                      ) : task.error_message ? (
                        <span className="log-error">{truncate(task.error_message, 60)}</span>
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                    </td>
                    <td>
                      <div className="btn-row">
                        {canCancel ? (
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled={busy}
                            onClick={() =>
                              void runAction(task.id, 'cancel', `任务 #${task.id} 已取消`)
                            }
                          >
                            取消
                          </button>
                        ) : null}
                        {canRetry ? (
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled={busy}
                            onClick={() =>
                              void runAction(task.id, 'retry', `任务 #${task.id} 已重新入队`)
                            }
                          >
                            重试
                          </button>
                        ) : null}
                        {canDelete ? (
                          <button
                            type="button"
                            className="btn btn-sm btn-danger"
                            disabled={busy}
                            onClick={() =>
                              void runAction(task.id, 'remove', `任务 #${task.id} 已删除`)
                            }
                          >
                            删除
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>

      {totalPages > 1 ? (
        <div className="toolbar" style={{ marginTop: 14 }}>
          <span className="text-sm text-muted">
            共 {total} 条，第 {page} / {totalPages} 页
          </span>
          <div className="spacer" />
          <button
            type="button"
            className="btn btn-sm"
            disabled={page <= 1}
            onClick={() => setPage((prev) => Math.max(1, prev - 1))}
          >
            上一页
          </button>
          <button
            type="button"
            className="btn btn-sm"
            disabled={page >= totalPages}
            onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))}
          >
            下一页
          </button>
        </div>
      ) : null}
    </>
  )
}
