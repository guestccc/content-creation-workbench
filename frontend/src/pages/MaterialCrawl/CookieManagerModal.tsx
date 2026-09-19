/**
 * Cookie 库管理弹窗：查看已保存的 Cookie、删除不需要的。
 *
 * 列表只显示 cookie_preview（截断预览），完整串不在这里展示 ——
 * 要拿来用就关掉弹窗从选择框里选。
 */

import { Modal, Popconfirm, Space, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'

import type { CrawlCookieListItem } from '../../types/crawlCookie'
import { PLATFORM_META } from '../../types/crawler'
import { formatDateTime } from '../../utils/format'

const { Text } = Typography

interface CookieManagerModalProps {
  open: boolean
  items: CrawlCookieListItem[]
  loading: boolean
  onClose: () => void
  onEdit: (id: number) => void
  onDelete: (id: number, name: string) => void
}

export default function CookieManagerModal({
  open,
  items,
  loading,
  onClose,
  onEdit,
  onDelete,
}: CookieManagerModalProps) {
  const columns: ColumnsType<CrawlCookieListItem> = [
    {
      title: '平台',
      dataIndex: 'platform',
      width: 90,
      render: (platform: string) => {
        const meta = PLATFORM_META[platform as keyof typeof PLATFORM_META]
        return <Tag color={meta?.color}>{meta?.label ?? platform}</Tag>
      },
    },
    { title: '名称', dataIndex: 'name', width: 120 },
    {
      title: 'Cookie 预览',
      dataIndex: 'cookie_preview',
      ellipsis: true,
      render: (text: string) => (
        <Text code style={{ fontSize: 12 }}>
          {text}
        </Text>
      ),
    },
    { title: '备注', dataIndex: 'remark', width: 140, ellipsis: true },
    {
      title: '保存时间',
      dataIndex: 'created_at',
      width: 160,
      render: (value: string) => formatDateTime(value),
    },
    {
      title: '操作',
      width: 110,
      render: (_, record) => (
        <Space size={12}>
          <a onClick={() => onEdit(record.id)}>编辑</a>
          <Popconfirm
            title={`删除「${record.name}」？`}
            description="删除后不可恢复。"
            okText="删除"
            cancelText="取消"
            onConfirm={() => onDelete(record.id, record.name)}
          >
            <a>删除</a>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Modal
      open={open}
      title="Cookie 库管理"
      footer={null}
      width={800}
      onCancel={onClose}
      destroyOnHidden
    >
      <Table
        rowKey="id"
        columns={columns}
        dataSource={items}
        loading={loading}
        pagination={false}
      />
    </Modal>
  )
}
