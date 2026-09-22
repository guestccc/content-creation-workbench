/**
 * 历史任务表里几个跨页面一致的列。
 *
 * 六个任务域的历史表都是「ID / 状态 / …中间各域自己的列… / 备注 / 创建时间 / 操作」，
 * 逐字一样的那几列做成工厂函数，各页面自己拼装数组。
 */

import { Button, Popconfirm, Space, Tag, Tooltip, Typography } from 'antd'
import type { ColumnType } from 'antd/es/table'
import type { MouseEvent } from 'react'

import type { UsePurgeFilesResult } from '../hooks'

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

/** 备注列要用到的最小行形状：备注就在列表行上，不必为编辑再取一次详情 */
export interface JobRowRemark {
  id: number
  /** 备注（用户自己写的标记；空串 / 缺失都表示没写） */
  remark?: string
}

/**
 * 备注列：单元格本身就是编辑入口 —— 有备注就显示备注（截断、悬浮看全文），
 * 没备注显示「＋ 添加备注」，点一下打开备注编辑弹窗。
 *
 * 截断交给单元格里的 Typography.Text（它自带 antd 样式的 tooltip），所以列上的
 * ellipsis 写 { showTitle: false }：把 rc-table 往 <td> 上写的原生 title 关掉，
 * 否则悬停时浏览器原生提示会和 antd 提示同时冒出来。
 *
 * 不用 Typography.Link：它的 ellipsis 只接受 boolean，给不了 { tooltip }；
 * 链接观感用 Text + 主题色自己给。
 */
export function jobRemarkColumn<T extends JobRowRemark>({
  onEdit,
}: {
  /** 点备注单元格：打开这条任务的备注编辑弹窗 */
  onEdit: (job: T) => void
}): ColumnType<T> {
  return {
    title: '备注',
    dataIndex: 'remark',
    // 窄列：再宽就要去挤「输入 / 输出目录 / 内容」那些真正需要空间的弹性列了
    width: 160,
    ellipsis: { showTitle: false },
    render: (_: unknown, record: T) => {
      const remark = record.remark?.trim() ?? ''
      const openEditor = (event: MouseEvent) => {
        // 挡一下冒泡：将来若有人给表加 onRow.onClick（点行看详情），
        //「点备注 = 改备注」应当优先
        event.stopPropagation()
        onEdit(record)
      }

      if (!remark) {
        return (
          <Text type="secondary" style={{ fontSize: 12, cursor: 'pointer' }} onClick={openEditor}>
            ＋ 添加备注
          </Text>
        )
      }
      return (
        <Text
          ellipsis={{ tooltip: remark }}
          style={{ fontSize: 12, cursor: 'pointer', color: 'var(--color-primary)' }}
          onClick={openEditor}
        >
          {remark}
        </Text>
      )
    },
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
 * 操作列：查看 / 取消 / 重试 / 删除。
 *
 * 跑着的任务给「取消」，跑完的给「删除」—— 两者互斥，同一条任务不会既取消又删除。
 *
 * 「重试」是可选的（`onRetry` 传了才有）：只有跑完、且还有没产出的条目时才亮。
 * 列表接口不带条目明细，所以判断只能靠行上的计数 —— 各页面用 canRetry 自己收窄
 * （背景看 failed_images/skipped_images，字幕看 failed_videos/skipped_videos，
 * 抓取这种没有条目级状态的看任务状态本身）。
 */
export function jobActionsColumn<T extends JobRowIdent>({
  isTerminal,
  onView,
  onCancel,
  onDelete,
  deleteDescription,
  purge,
  onRetry,
  canRetry,
}: {
  isTerminal: (job: T) => boolean
  onView: (jobId: number) => void
  onCancel: (jobId: number) => void
  onDelete: (jobId: number) => void
  /** 删除确认框里的说明（各页面产物不同，一句话说清删的是什么） */
  deleteDescription: string
  /** 「连同磁盘产物一起删」的勾选状态（页面的 usePurgeFiles()） */
  purge: UsePurgeFilesResult
  /** 重试这条任务里所有失败 / 跳过的条目；不传就没有「重试」这个按钮 */
  onRetry?: (job: T) => void
  /** 这条任务现在值不值得给「重试」（默认：终态且不是全成功） */
  canRetry?: (job: T) => boolean
}): ColumnType<T> {
  const retryable = (job: T): boolean =>
    onRetry !== undefined && isTerminal(job) && (canRetry ? canRetry(job) : true)

  return {
    title: '操作',
    // 四个操作按钮（查看 / 取消 / 重试 / 删除）并排，宽度按最宽的那种状态留够
    width: onRetry ? 220 : 180,
    render: (_: unknown, record: T) => (
      <Space size={4}>
        <Button type="link" style={{ padding: 0 }} onClick={() => onView(record.id)}>
          查看
        </Button>
        {retryable(record) && (
          <Button type="link" style={{ padding: 0 }} onClick={() => onRetry?.(record)}>
            重试
          </Button>
        )}
        {!isTerminal(record) ? (
          <Button type="link" style={{ padding: 0 }} onClick={() => onCancel(record.id)}>
            取消
          </Button>
        ) : (
          <Popconfirm
            title="删除这条任务记录？"
            description={
              <div>
                <div>{deleteDescription}</div>
                {purge.checkbox}
              </div>
            }
            okText="删除"
            cancelText="取消"
            onConfirm={() => onDelete(record.id)}
            // 打开即重置：「勾了又取消」的勾选不许残留到下一次确认
            onOpenChange={(open) => {
              if (open) {
                purge.reset()
              }
            }}
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
