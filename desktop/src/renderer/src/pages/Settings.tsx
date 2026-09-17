/** 设置：后端地址、轮询策略与发布器状态。 */

import { useCallback, useEffect, useState } from 'react'

import { CONFIG_LIMITS } from '@shared/constants'
import type { AppConfig } from '@shared/types'

import { call, toMessage } from '../api'

export default function SettingsPage(): JSX.Element {
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [draft, setDraft] = useState<AppConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [notice, setNotice] = useState<{ kind: 'success' | 'error' | 'info'; text: string } | null>(
    null
  )

  /** 读取当前配置 */
  const load = useCallback(async () => {
    setLoading(true)
    try {
      const current = await call(window.api.config.get())
      setConfig(current)
      setDraft(current)
    } catch (err) {
      setNotice({ kind: 'error', text: toMessage(err) })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  /** 探测后端连通性 */
  async function testConnection(): Promise<void> {
    if (!draft) {
      return
    }
    setTesting(true)
    setNotice(null)
    try {
      // 先保存再探测，保证测的就是即将生效的地址
      const saved = await call(window.api.config.update({ backendBaseUrl: draft.backendBaseUrl }))
      setConfig(saved)
      const result = await call(window.api.config.ping())
      setNotice({
        kind: 'success',
        text: `连接成功：后端版本 ${result.version}，数据库 ${result.database}`
      })
    } catch (err) {
      setNotice({ kind: 'error', text: `连接失败：${toMessage(err)}` })
    } finally {
      setTesting(false)
    }
  }

  /** 保存配置 */
  async function save(): Promise<void> {
    if (!draft) {
      return
    }
    setSaving(true)
    setNotice(null)
    try {
      const saved = await call(window.api.config.update(draft))
      setConfig(saved)
      setDraft(saved)
      setNotice({ kind: 'success', text: '设置已保存，新的轮询间隔与并发数立即生效' })
    } catch (err) {
      setNotice({ kind: 'error', text: toMessage(err) })
    } finally {
      setSaving(false)
    }
  }

  if (loading || !draft) {
    return <div className="loading">正在读取设置…</div>
  }

  const dirty = config ? JSON.stringify(config) !== JSON.stringify(draft) : false

  return (
    <>
      {notice ? <div className={`alert alert-${notice.kind}`}>{notice.text}</div> : null}

      <div className="card">
        <h2 className="card-title">后端服务</h2>
        <p className="card-desc">
          客户端通过该地址读取内容、读写账号与发布任务。实际的发布动作和凭证处理都在本机完成。
        </p>

        <div className="field">
          <label className="field-label" htmlFor="backend-url">
            服务地址
          </label>
          <input
            id="backend-url"
            type="text"
            value={draft.backendBaseUrl}
            placeholder="http://127.0.0.1:8000"
            onChange={(event) =>
              setDraft((prev) => (prev ? { ...prev, backendBaseUrl: event.target.value } : prev))
            }
          />
          <span className="field-hint">
            只支持 http / https；默认为本机 8000 端口，本地开发时后端启动即可。
          </span>
        </div>

        <div className="btn-row" style={{ marginTop: 16 }}>
          <button
            type="button"
            className="btn"
            onClick={() => void testConnection()}
            disabled={testing}
          >
            {testing ? '测试中…' : '保存并测试连接'}
          </button>
        </div>
      </div>

      <div className="card">
        <h2 className="card-title">执行策略</h2>
        <p className="card-desc">
          控制客户端认领任务的节奏。并发数过高容易触发平台的频率限制，建议保持较小值。
        </p>

        <div className="form-grid">
          <div className="field">
            <label className="field-label" htmlFor="poll-interval">
              轮询间隔（秒）
            </label>
            <input
              id="poll-interval"
              type="number"
              min={CONFIG_LIMITS.pollIntervalSeconds.min}
              max={CONFIG_LIMITS.pollIntervalSeconds.max}
              value={draft.pollIntervalSeconds}
              onChange={(event) =>
                setDraft((prev) =>
                  prev ? { ...prev, pollIntervalSeconds: Number(event.target.value) } : prev
                )
              }
            />
            <span className="field-hint">
              取值范围 {CONFIG_LIMITS.pollIntervalSeconds.min} ~{' '}
              {CONFIG_LIMITS.pollIntervalSeconds.max} 秒
            </span>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="claim-batch">
              单轮认领上限
            </label>
            <input
              id="claim-batch"
              type="number"
              min={CONFIG_LIMITS.claimBatchSize.min}
              max={CONFIG_LIMITS.claimBatchSize.max}
              value={draft.claimBatchSize}
              onChange={(event) =>
                setDraft((prev) =>
                  prev ? { ...prev, claimBatchSize: Number(event.target.value) } : prev
                )
              }
            />
            <span className="field-hint">每次向后端最多取走多少条任务</span>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="concurrency">
              并发执行数
            </label>
            <input
              id="concurrency"
              type="number"
              min={CONFIG_LIMITS.concurrency.min}
              max={CONFIG_LIMITS.concurrency.max}
              value={draft.concurrency}
              onChange={(event) =>
                setDraft((prev) =>
                  prev ? { ...prev, concurrency: Number(event.target.value) } : prev
                )
              }
            />
            <span className="field-hint">
              同时执行的任务数，上限 {CONFIG_LIMITS.concurrency.max}
            </span>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="mock-failure">
              模拟失败概率
            </label>
            <input
              id="mock-failure"
              type="number"
              min={CONFIG_LIMITS.mockFailureRate.min}
              max={CONFIG_LIMITS.mockFailureRate.max}
              step={0.05}
              value={draft.mockFailureRate}
              onChange={(event) =>
                setDraft((prev) =>
                  prev ? { ...prev, mockFailureRate: Number(event.target.value) } : prev
                )
              }
            />
            <span className="field-hint">
              仅对模拟发布器生效，调成 0.3 可以快速验证失败重试链路
            </span>
          </div>
        </div>

        <div className="checkbox-row" style={{ marginTop: 16 }}>
          <input
            id="auto-start"
            type="checkbox"
            checked={draft.autoStartScheduler}
            onChange={(event) =>
              setDraft((prev) =>
                prev ? { ...prev, autoStartScheduler: event.target.checked } : prev
              )
            }
          />
          <label htmlFor="auto-start">客户端启动后自动开始执行发布队列</label>
        </div>

        <div className="btn-row" style={{ marginTop: 18 }}>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void save()}
            disabled={saving || !dirty}
          >
            {saving ? '保存中…' : dirty ? '保存设置' : '已是最新'}
          </button>
          <button type="button" className="btn" onClick={() => void load()} disabled={saving}>
            放弃修改
          </button>
        </div>
      </div>

      <div className="card">
        <h2 className="card-title">平台发布器</h2>
        <p className="card-desc">
          发布动作由适配器完成，每个平台一个实现。当前尚未接入真实平台接口，
          全部平台暂时走模拟发布器，但认领、执行、重试、上报的整条链路是完整可用的。
        </p>

        <div className="alert alert-info">
          接入新平台只需在 <span className="mono">src/main/publishers/</span> 下新增一个实现
          <span className="mono"> Publisher </span>
          接口的适配器，并在 <span className="mono">registry.ts</span> 中注册，其余代码无需改动。
        </div>

        <div className="field">
          <label className="field-label">本机数据目录</label>
          <div className="btn-row">
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => void window.api.system.openDataDir()}
            >
              打开数据目录
            </button>
            <span className="field-hint">
              配置文件与加密后的凭证都存放在这里，可直接备份或彻底清除。
            </span>
          </div>
        </div>
      </div>
    </>
  )
}
