import { useEffect, useState, type FormEvent } from 'react'

import { ApiError } from '../api/client'
import { createContent, updateContent } from '../api/contents'
import type { Content, ContentPayload, ContentStatus } from '../types/content'
import { PLATFORM_OPTIONS, STATUS_META, STATUS_ORDER } from '../types/content'

interface ContentFormModalProps {
  /** 为 null 表示新建，否则为待编辑的内容 */
  editing: Content | null
  /** 关闭弹窗 */
  onClose: () => void
  /** 保存成功后回调，由父组件刷新列表 */
  onSaved: () => void
}

/** 表单内部状态：标签用字符串编辑，提交时再拆分为数组 */
interface FormState {
  title: string
  platform: string
  status: ContentStatus
  author: string
  tags: string
  body: string
}

const EMPTY_FORM: FormState = {
  title: '',
  platform: '',
  status: 'draft',
  author: '',
  tags: '',
  body: '',
}

/** 把内容实体转换为表单状态（标签用中文逗号连接，便于阅读） */
function toFormState(content: Content): FormState {
  return {
    title: content.title,
    platform: content.platform,
    status: content.status,
    author: content.author,
    tags: content.tags.join('，'),
    body: content.body,
  }
}

/** 把后端返回的字段级校验明细格式化为可读文本 */
function formatDetails(details: unknown): string {
  if (!Array.isArray(details)) {
    return ''
  }

  return details
    .map((item) => {
      if (item && typeof item === 'object' && 'field' in item && 'reason' in item) {
        const { field, reason } = item as { field: string; reason: string }
        return `${field}: ${reason}`
      }
      return ''
    })
    .filter(Boolean)
    .join('；')
}

/**
 * 内容创建 / 编辑弹窗。
 *
 * 同一个表单承担两种职责，由 editing 是否为空区分。
 */
export default function ContentFormModal({ editing, onClose, onSaved }: ContentFormModalProps) {
  const [form, setForm] = useState<FormState>(editing ? toFormState(editing) : EMPTY_FORM)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  // 支持 ESC 关闭弹窗
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  /** 更新表单中的单个字段 */
  const updateField = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    const title = form.title.trim()
    if (!title) {
      setError('标题不能为空')
      return
    }

    const payload: ContentPayload = {
      title,
      platform: form.platform.trim(),
      status: form.status,
      author: form.author.trim(),
      // 中英文逗号均作为分隔符，同时过滤掉空标签
      tags: form.tags
        .split(/[,，]/)
        .map((tag) => tag.trim())
        .filter(Boolean),
      body: form.body,
    }

    setSaving(true)
    setError('')

    try {
      if (editing) {
        await updateContent(editing.id, payload)
      } else {
        await createContent(payload)
      }
      onSaved()
    } catch (err) {
      if (err instanceof ApiError) {
        const detailText = formatDetails(err.details)
        setError(detailText ? `${err.message}（${detailText}）` : err.message)
      } else {
        setError('保存失败，请稍后重试')
      }
    } finally {
      setSaving(false)
    }
  }

  return (
    <div
      className="modal-mask"
      role="presentation"
      // 点击遮罩关闭，但点击弹窗内部不关闭
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          onClose()
        }
      }}
    >
      <div className="modal" role="dialog" aria-modal="true">
        <div className="modal__header">
          <h3 className="modal__title">{editing ? '编辑内容' : '新建内容'}</h3>
          <button type="button" className="modal__close" onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="modal__body">
            {error && <div className="alert alert--error">⚠️ {error}</div>}

            <div className="field">
              <label className="field__label" htmlFor="content-title">
                标题 <span className="field__required">*</span>
              </label>
              <input
                id="content-title"
                className="input"
                value={form.title}
                maxLength={200}
                placeholder="例如：秋季新品保温杯种草文案"
                onChange={(event) => updateField('title', event.target.value)}
              />
            </div>

            <div className="field-row">
              <div className="field">
                <label className="field__label" htmlFor="content-platform">
                  目标平台
                </label>
                <input
                  id="content-platform"
                  className="input"
                  list="platform-options"
                  value={form.platform}
                  maxLength={50}
                  placeholder="选择或直接输入"
                  onChange={(event) => updateField('platform', event.target.value)}
                />
                <datalist id="platform-options">
                  {PLATFORM_OPTIONS.map((platform) => (
                    <option key={platform} value={platform} />
                  ))}
                </datalist>
              </div>

              <div className="field">
                <label className="field__label" htmlFor="content-status">
                  状态
                </label>
                <select
                  id="content-status"
                  className="select"
                  value={form.status}
                  onChange={(event) => updateField('status', event.target.value as ContentStatus)}
                >
                  {STATUS_ORDER.map((status) => (
                    <option key={status} value={status}>
                      {STATUS_META[status].label}
                    </option>
                  ))}
                </select>
              </div>

              <div className="field">
                <label className="field__label" htmlFor="content-author">
                  作者
                </label>
                <input
                  id="content-author"
                  className="input"
                  value={form.author}
                  maxLength={100}
                  placeholder="创作者名称"
                  onChange={(event) => updateField('author', event.target.value)}
                />
              </div>
            </div>

            <div className="field">
              <label className="field__label" htmlFor="content-tags">
                标签
              </label>
              <input
                id="content-tags"
                className="input"
                value={form.tags}
                placeholder="用逗号分隔，例如：保温杯，办公好物"
                onChange={(event) => updateField('tags', event.target.value)}
              />
              <span className="field__hint">最多 10 个标签，重复标签会自动去除</span>
            </div>

            <div className="field">
              <label className="field__label" htmlFor="content-body">
                正文
              </label>
              <textarea
                id="content-body"
                className="textarea"
                value={form.body}
                placeholder="在这里撰写内容正文…"
                onChange={(event) => updateField('body', event.target.value)}
              />
            </div>
          </div>

          <div className="modal__footer">
            <button type="button" className="btn" onClick={onClose} disabled={saving}>
              取消
            </button>
            <button type="submit" className="btn btn--primary" disabled={saving}>
              {saving ? '保存中…' : '保存'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
