import { useCallback, useEffect, useState, type FormEvent } from 'react'

import { ApiError } from '../api/client'
import { deleteContent, fetchContents } from '../api/contents'
import ContentFormModal from '../components/ContentFormModal'
import type { Content, ContentStatus } from '../types/content'
import { PLATFORM_OPTIONS, STATUS_META, STATUS_ORDER } from '../types/content'
import { formatDateTime } from '../utils/format'

/** 每页条数 */
const PAGE_SIZE = 10

/**
 * 内容管理页。
 *
 * 提供列表查询、筛选、创建、编辑、删除的完整闭环。
 */
export default function ContentList() {
  // ---------- 列表数据 ----------
  const [items, setItems] = useState<Content[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)

  // ---------- 筛选条件 ----------
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [statusFilter, setStatusFilter] = useState<ContentStatus | ''>('')
  const [platformFilter, setPlatformFilter] = useState('')

  // ---------- 页面状态 ----------
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  // 弹窗状态：null 表示未打开；{ editing: null } 表示新建；{ editing: Content } 表示编辑
  const [modal, setModal] = useState<{ editing: Content | null } | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')

    try {
      const data = await fetchContents({
        page,
        page_size: PAGE_SIZE,
        keyword,
        status: statusFilter,
        platform: platformFilter,
      })
      setItems(data.items)
      setTotal(data.total)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '加载失败，请稍后重试')
    } finally {
      setLoading(false)
    }
  }, [page, keyword, statusFilter, platformFilter])

  useEffect(() => {
    void load()
  }, [load])

  // 操作成功提示 3 秒后自动消失
  useEffect(() => {
    if (!notice) {
      return
    }
    const timer = window.setTimeout(() => setNotice(''), 3000)
    return () => window.clearTimeout(timer)
  }, [notice])

  /** 提交搜索：回到第一页重新查询 */
  const handleSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setPage(1)
    setKeyword(keywordInput.trim())
  }

  /** 重置所有筛选条件 */
  const handleReset = () => {
    setKeywordInput('')
    setKeyword('')
    setStatusFilter('')
    setPlatformFilter('')
    setPage(1)
  }

  /** 删除内容（带二次确认） */
  const handleDelete = async (item: Content) => {
    if (!window.confirm(`确定要删除「${item.title}」吗？此操作不可恢复。`)) {
      return
    }

    try {
      await deleteContent(item.id)
      setNotice(`已删除「${item.title}」`)

      // 当前页只剩这一条且不是第一页时，回退一页避免停留在空页
      if (items.length === 1 && page > 1) {
        setPage(page - 1)
      } else {
        void load()
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '删除失败，请稍后重试')
    }
  }

  /** 弹窗保存成功后的回调 */
  const handleSaved = () => {
    const wasEditing = modal?.editing !== null && modal?.editing !== undefined
    setModal(null)
    setNotice(wasEditing ? '保存成功' : '创建成功')
    void load()
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <>
      <header className="page-header">
        <div>
          <h2 className="page-header__title">内容管理</h2>
          <p className="page-header__desc">管理你的所有创作内容，共 {total} 条</p>
        </div>
        <button
          type="button"
          className="btn btn--primary"
          onClick={() => setModal({ editing: null })}
        >
          + 新建内容
        </button>
      </header>

      {error && <div className="alert alert--error">⚠️ {error}</div>}
      {notice && (
        <div className="alert alert--warning" style={{ color: '#166534', background: '#f0fdf4', borderColor: '#bbf7d0' }}>
          ✓ {notice}
        </div>
      )}

      <section className="card">
        {/* 筛选工具栏 */}
        <form className="toolbar" onSubmit={handleSearch}>
          <input
            className="input"
            value={keywordInput}
            placeholder="搜索标题或正文…"
            onChange={(event) => setKeywordInput(event.target.value)}
          />

          <select
            className="select"
            value={statusFilter}
            onChange={(event) => {
              setStatusFilter(event.target.value as ContentStatus | '')
              setPage(1)
            }}
          >
            <option value="">全部状态</option>
            {STATUS_ORDER.map((status) => (
              <option key={status} value={status}>
                {STATUS_META[status].label}
              </option>
            ))}
          </select>

          <select
            className="select"
            value={platformFilter}
            onChange={(event) => {
              setPlatformFilter(event.target.value)
              setPage(1)
            }}
          >
            <option value="">全部平台</option>
            {PLATFORM_OPTIONS.map((platform) => (
              <option key={platform} value={platform}>
                {platform}
              </option>
            ))}
          </select>

          <button type="submit" className="btn">
            搜索
          </button>
          <button type="button" className="btn" onClick={handleReset}>
            重置
          </button>
        </form>

        {/* 列表主体 */}
        {loading ? (
          <div className="placeholder">加载中…</div>
        ) : items.length === 0 ? (
          <div className="placeholder">
            {keyword || statusFilter || platformFilter
              ? '没有匹配的内容，试试调整筛选条件'
              : '还没有任何内容，点击右上角「新建内容」开始创作'}
          </div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: '30%' }}>标题</th>
                <th>平台</th>
                <th>状态</th>
                <th>标签</th>
                <th>作者</th>
                <th>更新时间</th>
                <th style={{ width: 120 }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
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
                  <td>
                    {item.tags.length > 0 ? (
                      item.tags.map((tag) => (
                        <span className="tag" key={tag}>
                          {tag}
                        </span>
                      ))
                    ) : (
                      <span className="text-muted">—</span>
                    )}
                  </td>
                  <td>{item.author || <span className="text-muted">—</span>}</td>
                  <td className="text-muted">{formatDateTime(item.updated_at)}</td>
                  <td>
                    <div className="table__actions">
                      <button
                        type="button"
                        className="btn btn--link"
                        onClick={() => setModal({ editing: item })}
                      >
                        编辑
                      </button>
                      <button
                        type="button"
                        className="btn btn--link"
                        style={{ color: 'var(--color-danger)' }}
                        onClick={() => void handleDelete(item)}
                      >
                        删除
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {/* 分页 */}
        {!loading && total > 0 && (
          <div className="pagination">
            <span>
              第 {page} / {totalPages} 页，共 {total} 条
            </span>
            <button
              type="button"
              className="btn btn--sm"
              disabled={page <= 1}
              onClick={() => setPage(page - 1)}
            >
              上一页
            </button>
            <button
              type="button"
              className="btn btn--sm"
              disabled={page >= totalPages}
              onClick={() => setPage(page + 1)}
            >
              下一页
            </button>
          </div>
        )}
      </section>

      {modal && (
        <ContentFormModal
          editing={modal.editing}
          onClose={() => setModal(null)}
          onSaved={handleSaved}
        />
      )}
    </>
  )
}
