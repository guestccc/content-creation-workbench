import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Card, Flex, Input, Modal, Select, Space, Table, Tag, Typography, message } from 'antd'
import type { ColumnsType } from 'antd/es/table'

import { ApiError } from '../api/client'
import { deleteContent, fetchContents } from '../api/contents'
import ContentFormModal from '../components/ContentFormModal'
import type { Content, ContentStatus } from '../types/content'
import { PLATFORM_OPTIONS, STATUS_META, STATUS_ORDER } from '../types/content'
import { formatDateTime } from '../utils/format'

const { Title, Text } = Typography

/** 每页条数（后端分页） */
const PAGE_SIZE = 10

/**
 * 内容管理页。
 *
 * 提供列表查询、筛选、创建、编辑、删除的完整闭环。
 * 分页、表格、确认框都交给 antd：这几处的边界情况（翻页越界、空数据、
 * 键盘操作）它已经处理过了，自己写只会漏。
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

  // 弹窗状态：null 表示未打开；{ editing: null } 表示新建；{ editing: Content } 表示编辑
  const [modal, setModal] = useState<{ editing: Content | null } | null>(null)

  // antd 的 message / Modal.confirm 需要挂到当前 React 树上（useXxx 形式），
  // 直接用 message.xxx 静态方法会拿不到 ConfigProvider 的主题与语言
  const [messageApi, messageContext] = message.useMessage()
  const [confirmApi, confirmContext] = Modal.useModal()

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

  /** 提交搜索：回到第一页重新查询 */
  const handleSearch = () => {
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
    const confirmed = await confirmApi.confirm({
      title: `确定要删除「${item.title}」吗？`,
      content: '此操作不可恢复。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
    })
    if (!confirmed) {
      return
    }

    try {
      await deleteContent(item.id)
      messageApi.success(`已删除「${item.title}」`)

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
    // 提示交给 message，不再占用页面顶部的位置
    messageApi.success(wasEditing ? '保存成功' : '创建成功')
    void load()
  }

  const columns: ColumnsType<Content> = [
    {
      title: '标题',
      dataIndex: 'title',
      key: 'title',
      width: '30%',
      render: (title: string) => <Text strong>{title}</Text>,
    },
    {
      title: '平台',
      dataIndex: 'platform',
      key: 'platform',
      render: (platform: string) => platform || <Text type="secondary">—</Text>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      render: (status: ContentStatus) => (
        <Tag color={STATUS_META[status].color}>{STATUS_META[status].label}</Tag>
      ),
    },
    {
      title: '标签',
      dataIndex: 'tags',
      key: 'tags',
      render: (tags: string[]) =>
        tags.length > 0 ? (
          <>
            {tags.map((tag) => (
              <Tag key={tag}>{tag}</Tag>
            ))}
          </>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: '作者',
      dataIndex: 'author',
      key: 'author',
      render: (author: string) => author || <Text type="secondary">—</Text>,
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      render: (value: string) => <Text type="secondary">{formatDateTime(value)}</Text>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 130,
      render: (_, item) => (
        <Space size={0}>
          <Button type="link" size="small" onClick={() => setModal({ editing: item })}>
            编辑
          </Button>
          <Button type="link" size="small" danger onClick={() => void handleDelete(item)}>
            删除
          </Button>
        </Space>
      ),
    },
  ]

  const filtered = Boolean(keyword || statusFilter || platformFilter)

  return (
    <Flex vertical gap={22}>
      {messageContext}
      {confirmContext}

      <Flex justify="space-between" align="flex-start" gap={16}>
        <div>
          <Title level={3} style={{ margin: 0 }}>
            内容管理
          </Title>
          <Text type="secondary">管理你的所有创作内容，共 {total} 条</Text>
        </div>
        <Button type="primary" onClick={() => setModal({ editing: null })}>
          + 新建内容
        </Button>
      </Flex>

      {error && <Alert type="error" showIcon message={error} />}

      <Card>
        {/* 筛选工具栏 */}
        <Flex wrap gap={10} style={{ marginBottom: 16 }}>
          <Input
            value={keywordInput}
            placeholder="搜索标题或正文…"
            allowClear
            style={{ maxWidth: 260 }}
            onChange={(event) => setKeywordInput(event.target.value)}
            onPressEnter={handleSearch}
          />

          <Select
            value={statusFilter}
            style={{ minWidth: 140 }}
            onChange={(value: ContentStatus | '') => {
              setStatusFilter(value)
              setPage(1)
            }}
            options={[
              { value: '', label: '全部状态' },
              ...STATUS_ORDER.map((status) => ({
                value: status,
                label: STATUS_META[status].label,
              })),
            ]}
          />

          <Select
            value={platformFilter}
            style={{ minWidth: 140 }}
            onChange={(value: string) => {
              setPlatformFilter(value)
              setPage(1)
            }}
            options={[
              { value: '', label: '全部平台' },
              ...PLATFORM_OPTIONS.map((platform) => ({ value: platform, label: platform })),
            ]}
          />

          <Button onClick={handleSearch}>搜索</Button>
          <Button onClick={handleReset}>重置</Button>
        </Flex>

        {/* 列表主体：分页交给 Table 自己管，它会在翻页时回调 onChange */}
        <Table<Content>
          rowKey="id"
          size="middle"
          columns={columns}
          dataSource={items}
          loading={loading}
          pagination={{
            current: page,
            pageSize: PAGE_SIZE,
            total,
            showSizeChanger: false,
            // 刻意不设 showTotal：总数已经写在页面副标题里，两处重复没有意义
            onChange: setPage,
          }}
          locale={{
            emptyText: filtered
              ? '没有匹配的内容，试试调整筛选条件'
              : '还没有任何内容，点击右上角「新建内容」开始创作',
          }}
        />
      </Card>

      {modal && (
        <ContentFormModal
          editing={modal.editing}
          onClose={() => setModal(null)}
          onSaved={handleSaved}
        />
      )}
    </Flex>
  )
}
