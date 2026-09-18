/**
 * 历史任务卡片：标题 + 刷新按钮 + 表格。
 *
 * 三个任务型页面的历史区外壳一样，差异全在列定义上，所以列由页面传进来。
 */

import type { ReactNode } from 'react'
import { HistoryOutlined } from '@ant-design/icons'
import { Button, Card, Empty, Space, Table } from 'antd'
import type { ColumnsType, TableProps } from 'antd/es/table'

interface HistoryCardProps<T extends object> {
  columns: ColumnsType<T>
  dataSource: T[]
  loading: boolean
  /** 传了才显示「刷新」按钮 */
  onRefresh?: () => void
  emptyText?: ReactNode
  pagination?: TableProps<T>['pagination']
}

export default function HistoryCard<T extends object>({
  columns,
  dataSource,
  loading,
  onRefresh,
  emptyText = <Empty description="还没有任务记录" />,
  pagination = false,
}: HistoryCardProps<T>) {
  return (
    <Card
      title={
        <Space size={8}>
          <HistoryOutlined style={{ color: 'var(--color-primary)' }} />
          历史任务
        </Space>
      }
      extra={
        onRefresh ? (
          <Button onClick={onRefresh} loading={loading}>
            刷新
          </Button>
        ) : undefined
      }
    >
      <Table<T>
        rowKey="id"
        loading={loading}
        dataSource={dataSource}
        columns={columns}
        pagination={pagination}
        locale={{ emptyText }}
      />
    </Card>
  )
}
