/**
 * 历史任务表里几个跨页面一致的列。
 *
 * 镜头分割与字幕提取的历史表逐列同构（ID / 状态 / 输入 / 视频数 / 创建时间 / 操作），
 * 差异只在中间那两列，所以共用的这几列做成工厂函数，各页面自己拼装数组。
 */

import { Button, Popconfirm, Space, Tag, Tooltip, Typography } from 'antd'
import type { ColumnType } from 'antd/es/table'

const { Text } = Typography

/** 状态展示配置（三个域的 JOB_STATUS_META 形状一致，文案各自定义） */
export interface JobStatusMeta {
  label: string
  color: string
  hint: string
}

/** 历史表所有行都有的字段 */
export interface JobRowBase {
  id: number
  status: string
  input_path: string
  total_videos: number
  created_at: string
}

/** 任务行的最小形状：各页面用得到的列各自再收窄约束 */
export interface JobRowIdent {
  id: number
  status: string
  /** 失败/部分成功时的原因摘要；有就在状态列的悬停提示里优先展示 */
  error_message?: string
}

/** ID 列 */
export function jobIdColumn<T extends JobRowIdent>(): ColumnType<T> {
  return { title: 'ID', dataIndex: 'id', width: 64 }
}

/** 状态列：颜色 + 悬停给出失败原因（没有原因时退回这个状态的含义） */
export function jobStatusColumn<T extends JobRowIdent>(
  meta: Record<string, JobStatusMeta>,
): ColumnType<T> {
  return {
    title: '状态',
    dataIndex: 'status',
    width: 96,
    render: (status: string, record) => (
      <Tooltip title={record.error_message || meta[status].hint}>
        <Tag color={meta[status].color}>{meta[status].label}</Tag>
      </Tooltip>
    ),
  }
}

/** 输入目录列：只显示末级目录名，完整路径放悬停提示里 */
export function jobInputColumn<T extends { input_path: string }>(): ColumnType<T> {
  return {
    title: '输入',
    dataIndex: 'input_path',
    ellipsis: true,
    render: (value: string) => (
      <Tooltip title={value}>
        <Text style={{ fontSize: 12 }}>{value.split('/').pop()}</Text>
      </Tooltip>
    ),
  }
}

/** 创建时间列 */
export function jobCreatedColumn<T extends { created_at: string }>(): ColumnType<T> {
  return {
    title: '创建时间',
    dataIndex: 'created_at',
    width: 150,
    render: (value: string) => (
      <Text style={{ fontSize: 12 }}>{new Date(value).toLocaleString('zh-CN')}</Text>
    ),
  }
}

/**
 * 操作列：查看 / 取消 / 删除。
 *
 * 跑着的任务给「取消」，跑完的给「删除」—— 两者互斥，同一条任务不会既取消又删除。
 */
export function jobActionsColumn<T extends JobRowIdent>({
  isTerminal,
  onView,
  onCancel,
  onDelete,
  deleteDescription,
}: {
  isTerminal: (job: T) => boolean
  onView: (jobId: number) => void
  onCancel: (jobId: number) => void
  onDelete: (jobId: number) => void
  /** 删除确认框里的说明（各页面产物不同，说清「删的是什么、留的是什么」） */
  deleteDescription: string
}): ColumnType<T> {
  return {
    title: '操作',
    // 三个操作按钮（查看 / 取消 / 删除）并排，宽度按最宽的那种状态留够
    width: 180,
    render: (_: unknown, record: T) => (
      <Space size={4}>
        <Button type="link" style={{ padding: 0 }} onClick={() => onView(record.id)}>
          查看
        </Button>
        {!isTerminal(record) ? (
          <Button type="link" style={{ padding: 0 }} onClick={() => onCancel(record.id)}>
            取消
          </Button>
        ) : (
          <Popconfirm
            title="删除这条任务记录？"
            description={deleteDescription}
            okText="删除"
            cancelText="取消"
            onConfirm={() => onDelete(record.id)}
          >
            <Button type="link" danger style={{ padding: 0 }}>
              删除
            </Button>
          </Popconfirm>
        )}
      </Space>
    ),
  }
}
