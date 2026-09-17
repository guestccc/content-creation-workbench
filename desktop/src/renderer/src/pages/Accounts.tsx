/** 账号与凭证：维护平台账号，并在本机加密保存登录凭证。 */

import { useCallback, useEffect, useState } from 'react'

import { ACCOUNT_STATUS_META, CREDENTIAL_TYPE_META, SUPPORTED_PLATFORMS } from '@shared/constants'
import type {
  Account,
  AccountPayload,
  AccountStatus,
  CredentialPayload,
  CredentialStoreStatus,
  CredentialSummary,
  CredentialType
} from '@shared/types'

import { call, toMessage } from '../api'
import StatusBadge from '../components/StatusBadge'
import { formatDateTime } from '../utils/format'

/** 空账号表单 */
const EMPTY_ACCOUNT: AccountPayload = {
  platform: SUPPORTED_PLATFORMS[0],
  nickname: '',
  account_uid: '',
  status: 'active',
  remark: ''
}

/** 空凭证表单 */
const EMPTY_CREDENTIAL = {
  type: 'cookie' as CredentialType,
  secret: '',
  username: '',
  expiresAt: ''
}

export default function AccountsPage(): JSX.Element {
  const [accounts, setAccounts] = useState<Account[]>([])
  const [summaries, setSummaries] = useState<Record<number, CredentialSummary | null>>({})
  const [storeStatus, setStoreStatus] = useState<CredentialStoreStatus | null>(null)

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState<{ kind: 'success' | 'error' | 'info'; text: string } | null>(
    null
  )

  // 账号表单
  const [accountForm, setAccountForm] = useState<AccountPayload>(EMPTY_ACCOUNT)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [submitting, setSubmitting] = useState(false)

  // 凭证表单
  const [credentialAccountId, setCredentialAccountId] = useState<number | null>(null)
  const [credentialForm, setCredentialForm] = useState(EMPTY_CREDENTIAL)
  const [savingCredential, setSavingCredential] = useState(false)

  /** 拉取账号列表与凭证状态 */
  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [accountData, status] = await Promise.all([
        call(window.api.accounts.list()),
        call(window.api.credentials.status())
      ])
      setAccounts(accountData.items)
      setStoreStatus(status)

      // 逐个拉取凭证摘要：只拿到脱敏预览，主进程不会把明文交给界面
      const entries = await Promise.all(
        accountData.items.map(async (account) => {
          try {
            const summary = await call(window.api.credentials.summary(account.id))
            return [account.id, summary] as const
          } catch {
            return [account.id, null] as const
          }
        })
      )
      setSummaries(Object.fromEntries(entries))
    } catch (err) {
      setError(toMessage(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  /** 新增或更新账号 */
  async function submitAccount(): Promise<void> {
    if (!accountForm.nickname.trim()) {
      setNotice({ kind: 'error', text: '账号昵称不能为空' })
      return
    }

    setSubmitting(true)
    setNotice(null)
    try {
      if (editingId !== null) {
        await call(window.api.accounts.update(editingId, accountForm))
        setNotice({ kind: 'success', text: '账号信息已更新' })
      } else {
        await call(window.api.accounts.create(accountForm))
        setNotice({ kind: 'success', text: '账号已添加' })
      }
      setAccountForm(EMPTY_ACCOUNT)
      setEditingId(null)
      await load()
    } catch (err) {
      setNotice({ kind: 'error', text: toMessage(err) })
    } finally {
      setSubmitting(false)
    }
  }

  /** 删除账号（同时会清掉本机凭证） */
  async function removeAccount(account: Account): Promise<void> {
    setNotice(null)
    try {
      await call(window.api.accounts.remove(account.id))
      setNotice({
        kind: 'success',
        text: `账号「${account.nickname}」已删除，本机保存的凭证一并清除`
      })
      await load()
    } catch (err) {
      setNotice({ kind: 'error', text: toMessage(err) })
    }
  }

  /** 保存凭证到本机加密存储 */
  async function saveCredential(): Promise<void> {
    if (credentialAccountId === null) {
      return
    }
    if (!credentialForm.secret.trim()) {
      setNotice({ kind: 'error', text: '凭证内容不能为空' })
      return
    }
    if (credentialForm.type === 'password' && !credentialForm.username.trim()) {
      setNotice({ kind: 'error', text: '账号密码类型必须填写账号名' })
      return
    }

    setSavingCredential(true)
    setNotice(null)
    try {
      const payload: CredentialPayload = {
        type: credentialForm.type,
        secret: credentialForm.secret.trim(),
        username: credentialForm.username.trim() || undefined,
        expiresAt: credentialForm.expiresAt || null
      }
      const summary = await call(window.api.credentials.save(credentialAccountId, payload))
      // 保存成功后立即清空表单里的明文，避免残留在界面上
      setCredentialForm(EMPTY_CREDENTIAL)
      setCredentialAccountId(null)
      setSummaries((prev) => ({ ...prev, [summary.accountId]: summary }))
      setNotice({
        kind: 'success',
        text: `凭证已使用系统加密保存（预览：${summary.maskedSecret}），不会上传到服务端`
      })
    } catch (err) {
      setNotice({ kind: 'error', text: toMessage(err) })
    } finally {
      setSavingCredential(false)
    }
  }

  /** 清除某账号的本机凭证 */
  async function removeCredential(accountId: number, nickname: string): Promise<void> {
    setNotice(null)
    try {
      await call(window.api.credentials.remove(accountId))
      setSummaries((prev) => ({ ...prev, [accountId]: null }))
      setNotice({ kind: 'success', text: `已清除「${nickname}」在本机保存的凭证` })
    } catch (err) {
      setNotice({ kind: 'error', text: toMessage(err) })
    }
  }

  /** 切换到凭证编辑状态 */
  function startCredentialEdit(accountId: number): void {
    setCredentialAccountId(accountId)
    setCredentialForm(EMPTY_CREDENTIAL)
    setNotice(null)
  }

  const editingAccount = accounts.find((account) => account.id === credentialAccountId) ?? null

  return (
    <>
      {storeStatus ? (
        <div className={`alert alert-${storeStatus.available ? 'info' : 'warning'}`}>
          {storeStatus.message}
          {storeStatus.available ? (
            <>
              。凭证存放在本机应用数据目录，由系统钥匙串托管密钥，
              <button
                type="button"
                className="link"
                onClick={() => void window.api.system.openDataDir()}
              >
                打开目录
              </button>
              。
            </>
          ) : (
            <>。在系统加密恢复可用之前，凭证保存功能将保持禁用状态。</>
          )}
        </div>
      ) : null}

      {error ? (
        <div className="alert alert-error">
          加载失败：{error}
          <br />
          请确认后端服务已启动，并在「设置」页检查服务地址。
        </div>
      ) : null}

      {notice ? <div className={`alert alert-${notice.kind}`}>{notice.text}</div> : null}

      <div className="card">
        <h2 className="card-title">{editingId !== null ? '编辑账号' : '添加账号'}</h2>
        <p className="card-desc">
          这里只登记账号元信息（平台、昵称、状态）。登录凭证在下方单独保存到本机，
          不经过后端，也不会写入数据库。
        </p>

        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="account-platform">
              平台
            </label>
            <select
              id="account-platform"
              value={accountForm.platform}
              onChange={(event) =>
                setAccountForm((prev) => ({ ...prev, platform: event.target.value }))
              }
            >
              {SUPPORTED_PLATFORMS.map((platform) => (
                <option key={platform} value={platform}>
                  {platform}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="account-nickname">
              账号昵称
            </label>
            <input
              id="account-nickname"
              type="text"
              value={accountForm.nickname}
              placeholder="例如：小明的好物分享"
              onChange={(event) =>
                setAccountForm((prev) => ({ ...prev, nickname: event.target.value }))
              }
            />
            <span className="field-hint">同一平台下昵称不能重复</span>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="account-uid">
              平台账号 ID（可选）
            </label>
            <input
              id="account-uid"
              type="text"
              value={accountForm.account_uid ?? ''}
              onChange={(event) =>
                setAccountForm((prev) => ({ ...prev, account_uid: event.target.value }))
              }
            />
          </div>

          <div className="field">
            <label className="field-label" htmlFor="account-status">
              状态
            </label>
            <select
              id="account-status"
              value={accountForm.status ?? 'active'}
              onChange={(event) =>
                setAccountForm((prev) => ({
                  ...prev,
                  status: event.target.value as AccountStatus
                }))
              }
            >
              {(Object.keys(ACCOUNT_STATUS_META) as AccountStatus[]).map((status) => (
                <option key={status} value={status}>
                  {ACCOUNT_STATUS_META[status].label}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="account-remark">
              备注（可选）
            </label>
            <input
              id="account-remark"
              type="text"
              value={accountForm.remark ?? ''}
              onChange={(event) =>
                setAccountForm((prev) => ({ ...prev, remark: event.target.value }))
              }
            />
          </div>
        </div>

        <div className="btn-row" style={{ marginTop: 18 }}>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void submitAccount()}
            disabled={submitting}
          >
            {submitting ? '提交中…' : editingId !== null ? '保存修改' : '添加账号'}
          </button>
          {editingId !== null ? (
            <button
              type="button"
              className="btn"
              onClick={() => {
                setEditingId(null)
                setAccountForm(EMPTY_ACCOUNT)
              }}
            >
              取消编辑
            </button>
          ) : null}
        </div>
      </div>

      {editingAccount ? (
        <div className="card">
          <h2 className="card-title">
            保存凭证 · {editingAccount.nickname}（{editingAccount.platform}）
          </h2>
          <p className="card-desc">
            凭证经系统加密后存放在本机，界面上只展示脱敏预览。
            保存后原值无法再被读出，需要更换时直接重新保存即可。
          </p>

          <div className="form-grid">
            <div className="field">
              <label className="field-label" htmlFor="credential-type">
                凭证类型
              </label>
              <select
                id="credential-type"
                value={credentialForm.type}
                onChange={(event) =>
                  setCredentialForm((prev) => ({
                    ...prev,
                    type: event.target.value as CredentialType
                  }))
                }
              >
                {(Object.keys(CREDENTIAL_TYPE_META) as CredentialType[]).map((type) => (
                  <option key={type} value={type}>
                    {CREDENTIAL_TYPE_META[type].label}
                  </option>
                ))}
              </select>
              <span className="field-hint">{CREDENTIAL_TYPE_META[credentialForm.type].hint}</span>
            </div>

            {credentialForm.type === 'password' ? (
              <div className="field">
                <label className="field-label" htmlFor="credential-username">
                  账号名
                </label>
                <input
                  id="credential-username"
                  type="text"
                  value={credentialForm.username}
                  onChange={(event) =>
                    setCredentialForm((prev) => ({ ...prev, username: event.target.value }))
                  }
                />
              </div>
            ) : null}

            <div className="field">
              <label className="field-label" htmlFor="credential-expires">
                过期时间（可选）
              </label>
              <input
                id="credential-expires"
                type="datetime-local"
                value={credentialForm.expiresAt}
                onChange={(event) =>
                  setCredentialForm((prev) => ({ ...prev, expiresAt: event.target.value }))
                }
              />
              <span className="field-hint">仅作提醒，到期后不会自动清除凭证</span>
            </div>
          </div>

          <div className="field" style={{ marginTop: 14 }}>
            <label className="field-label" htmlFor="credential-secret">
              {credentialForm.type === 'password' ? '密码' : '凭证内容'}
            </label>
            <textarea
              id="credential-secret"
              value={credentialForm.secret}
              placeholder={
                credentialForm.type === 'cookie'
                  ? '粘贴完整的 Cookie 串'
                  : credentialForm.type === 'token'
                    ? '粘贴访问令牌'
                    : '输入密码'
              }
              onChange={(event) =>
                setCredentialForm((prev) => ({ ...prev, secret: event.target.value }))
              }
            />
            <span className="field-hint">
              内容仅保存在本机，提交后立即从界面清除，请自行留存备份。
            </span>
          </div>

          <div className="btn-row" style={{ marginTop: 18 }}>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void saveCredential()}
              disabled={savingCredential || !storeStatus?.available}
            >
              {savingCredential ? '加密保存中…' : '加密保存到本机'}
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => {
                setCredentialAccountId(null)
                setCredentialForm(EMPTY_CREDENTIAL)
              }}
            >
              取消
            </button>
          </div>
        </div>
      ) : null}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th style={{ width: 60 }}>ID</th>
              <th>平台</th>
              <th>昵称</th>
              <th style={{ width: 90 }}>状态</th>
              <th style={{ width: 220 }}>本机凭证</th>
              <th>备注</th>
              <th style={{ width: 220 }}>操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={7}>
                  <div className="empty">正在加载…</div>
                </td>
              </tr>
            ) : accounts.length === 0 ? (
              <tr>
                <td colSpan={7}>
                  <div className="empty">还没有账号，使用上方表单添加第一个。</div>
                </td>
              </tr>
            ) : (
              accounts.map((account) => {
                const summary = summaries[account.id] ?? null
                const meta = ACCOUNT_STATUS_META[account.status]
                return (
                  <tr key={account.id}>
                    <td className="mono">{account.id}</td>
                    <td>{account.platform}</td>
                    <td>{account.nickname}</td>
                    <td>
                      <StatusBadge label={meta.label} color={meta.color} />
                    </td>
                    <td className="text-sm">
                      {summary ? (
                        <>
                          <StatusBadge label="已保存" color="#16a34a" />
                          <div className="text-muted" style={{ marginTop: 4 }}>
                            <span className="mono">{summary.maskedSecret || '****'}</span>
                            {' · '}
                            更新于 {formatDateTime(summary.updatedAt)}
                          </div>
                        </>
                      ) : (
                        <StatusBadge label="未保存" color="#d97706" />
                      )}
                    </td>
                    <td className="text-sm text-muted">{account.remark || '—'}</td>
                    <td>
                      <div className="btn-row">
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() => startCredentialEdit(account.id)}
                        >
                          {summary ? '更换凭证' : '保存凭证'}
                        </button>
                        {summary ? (
                          <button
                            type="button"
                            className="btn btn-sm"
                            onClick={() => void removeCredential(account.id, account.nickname)}
                          >
                            清除
                          </button>
                        ) : null}
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() => {
                            setEditingId(account.id)
                            setAccountForm({
                              platform: account.platform,
                              nickname: account.nickname,
                              account_uid: account.account_uid,
                              status: account.status,
                              remark: account.remark
                            })
                            window.scrollTo({ top: 0, behavior: 'smooth' })
                          }}
                        >
                          编辑
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm btn-danger"
                          onClick={() => void removeAccount(account)}
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>
    </>
  )
}
