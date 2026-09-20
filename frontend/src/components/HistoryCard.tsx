/**
 * 历史任务卡片：标题 + 刷新按钮 + 表格。
 *
 * 任务型页面的历史区外壳一样，差异全在列定义上，所以列由页面传进来。
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
  /** 传了就启用行多选（批量删除用） */
  rowSelection?: TableProps<T>['rowSelection']
  /** 列多的表传 { x: 总宽 }：窄屏出横向滚动，别把弹性列压没 */
  scroll?: TableProps<T>['scroll']
  /** 追加在「刷新」按钮左边的操作区（批量删除按钮放这） */
  extra?: ReactNode
}

export default function HistoryCard<T extends object>({
  columns,
  dataSource,
  loading,
  onRefresh,
  emptyText = <Empty description="还没有任务记录" />,
  pagination = false,
  rowSelection,
  scroll,
  extra,
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
        onRefresh || extra ? (
          <Space size={8}>
            {extra}
            {onRefresh && (
              <Button onClick={onRefresh} loading={loading}>
                刷新
              </Button>
            )}
          </Space>
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
        rowSelection={rowSelection}
        scroll={scroll}
      />
    </Card>
  )
}
