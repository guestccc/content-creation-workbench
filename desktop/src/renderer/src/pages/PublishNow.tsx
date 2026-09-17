/** 一键分发：把一条内容批量排入多个账号的发布队列。 */

import { useCallback, useEffect, useState } from 'react'

import type { Account, Content } from '@shared/types'

import { call, toMessage } from '../api'
import StatusBadge from '../components/StatusBadge'
import { ACCOUNT_STATUS_META } from '@shared/constants'
import { localInputToUtcIso, truncate } from '../utils/format'

interface Props {
  /** 任务创建成功后通知外层刷新角标 */
  onTaskCreated: () => void
}

export default function PublishNowPage({ onTaskCreated }: Props): JSX.Element {
  const [contents, setContents] = useState<Content[]>([])
  const [accounts, setAccounts] = useState<Account[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')

  const [contentId, setContentId] = useState<number | null>(null)
  const [selectedAccountIds, setSelectedAccountIds] = useState<number[]>([])
  const [scheduledAt, setScheduledAt] = useState('')
  const [maxRetries, setMaxRetries] = useState(2)

  const [submitting, setSubmitting] = useState(false)
  const [notice, setNotice] = useState<{ kind: 'success' | 'error'; text: string } | null>(null)

  /** 载入内容与账号列表 */
  const load = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      const [contentData, accountData] = await Promise.all([
        call(window.api.contents.list({ page: 1, page_size: 100 })),
        call(window.api.accounts.list())
      ])
      setContents(contentData.items)
      setAccounts(accountData.items)
      // 默认选中第一条内容，减少一次点击
      setContentId((prev) => prev ?? contentData.items[0]?.id ?? null)
    } catch (error) {
      setLoadError(toMessage(error))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  /** 勾选 / 取消勾选账号 */
  function toggleAccount(id: number): void {
    setSelectedAccountIds((prev) =>
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]
    )
  }

  /** 全选 / 清空 */
  function toggleAll(): void {
    setSelectedAccountIds((prev) =>
      prev.length === accounts.length ? [] : accounts.map((account) => account.id)
    )
  }

  /** 提交批量创建 */
  async function handleSubmit(): Promise<void> {
    if (!contentId) {
      setNotice({ kind: 'error', text: '请先选择要发布的内容' })
      return
    }
    if (selectedAccountIds.length === 0) {
      setNotice({ kind: 'error', text: '请至少选择一个目标账号' })
      return
    }

    setSubmitting(true)
    setNotice(null)
    try {
      const tasks = await call(
        window.api.tasks.batchCreate({
          content_id: contentId,
          account_ids: selectedAccountIds,
          scheduled_at: localInputToUtcIso(scheduledAt),
          max_retries: maxRetries
        })
      )

      setNotice({
        kind: 'success',
        text: `已创建 ${tasks.length} 条发布任务${
          scheduledAt ? '，将在计划时间到达后被认领执行' : '，到「发布任务」页启动执行即可开始'
        }`
      })
      setSelectedAccountIds([])
      onTaskCreated()
    } catch (error) {
      setNotice({ kind: 'error', text: toMessage(error) })
    } finally {
      setSubmitting(false)
    }
  }

  const selectedContent = contents.find((item) => item.id === contentId) ?? null

  if (loading) {
    return <div className="loading">正在加载内容与账号…</div>
  }

  return (
    <>
      {loadError ? (
        <div className="alert alert-error">
          加载失败：{loadError}
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
        <h2 className="card-title">1. 选择内容</h2>
        <p className="card-desc">内容来自工作台后端，选中后会被分发到下方勾选的账号。</p>

        {contents.length === 0 ? (
          <div className="alert alert-info">
            后端还没有任何内容，请先在 Web 工作台创建内容后再回到这里分发。
          </div>
        ) : (
          <div className="field">
            <label className="field-label" htmlFor="content-select">
              待发布内容
            </label>
            <select
              id="content-select"
              value={contentId ?? ''}
              onChange={(event) => setContentId(Number(event.target.value))}
            >
              {contents.map((content) => (
                <option key={content.id} value={content.id}>
                  #{content.id} {truncate(content.title, 40)}
                </option>
              ))}
            </select>
            {selectedContent ? (
              <span className="field-hint">
                标签：{selectedContent.tags.length ? selectedContent.tags.join('、') : '无'} ·
                作者：{selectedContent.author || '未填写'}
              </span>
            ) : null}
          </div>
        )}
      </div>

      <div className="card">
        <div className="toolbar">
          <h2 className="card-title" style={{ margin: 0 }}>
            2. 选择目标账号
          </h2>
          <div className="spacer" />
          <span className="text-sm text-muted">
            已选 {selectedAccountIds.length} / {accounts.length}
          </span>
          <button type="button" className="btn btn-sm" onClick={toggleAll} disabled={!accounts.length}>
            {selectedAccountIds.length === accounts.length && accounts.length > 0
              ? '清空选择'
              : '全选'}
          </button>
        </div>
        <p className="card-desc">
          一个账号会生成一条独立任务，可分别查看执行结果与失败原因。
        </p>

        {accounts.length === 0 ? (
          <div className="alert alert-info">
            还没有平台账号，请先到「账号与凭证」页添加。
          </div>
        ) : (
          <div className="account-picker">
            {accounts.map((account) => {
              const meta = ACCOUNT_STATUS_META[account.status]
              const selected = selectedAccountIds.includes(account.id)
              return (
                <label
                  key={account.id}
                  className={`account-option${selected ? ' selected' : ''}`}
                >
                  <input
                    type="checkbox"
                    checked={selected}
                    onChange={() => toggleAccount(account.id)}
                  />
                  <span className="account-option-body">
                    <span className="account-option-name">{account.nickname}</span>
                    <span className="account-option-meta">
                      {account.platform}
                      {' · '}
                      <StatusBadge label={meta.label} color={meta.color} />
                    </span>
                  </span>
                </label>
              )
            })}
          </div>
        )}
      </div>

      <div className="card">
        <h2 className="card-title">3. 执行策略</h2>
        <p className="card-desc">
          计划时间留空表示立即执行；客户端只有在处于「运行中」时才会认领任务。
        </p>

        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="scheduled-at">
              计划执行时间（可选）
            </label>
            <input
              id="scheduled-at"
              type="datetime-local"
              value={scheduledAt}
              onChange={(event) => setScheduledAt(event.target.value)}
            />
            <span className="field-hint">按本机时区填写，提交时会自动换算为 UTC</span>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="max-retries">
              失败自动重试次数
            </label>
            <input
              id="max-retries"
              type="number"
              min={0}
              max={10}
              value={maxRetries}
              onChange={(event) => setMaxRetries(Number(event.target.value))}
            />
            <span className="field-hint">重试额度用尽后任务转为失败终态</span>
          </div>
        </div>

        <div className="btn-row" style={{ marginTop: 18 }}>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void handleSubmit()}
            disabled={submitting || !contentId || selectedAccountIds.length === 0}
          >
            {submitting ? '创建中…' : `创建 ${selectedAccountIds.length || ''} 条发布任务`}
          </button>
          <button type="button" className="btn" onClick={() => void load()} disabled={submitting}>
            刷新数据
          </button>
        </div>
      </div>
    </>
  )
}
