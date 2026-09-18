/**
 * 任务进度卡片。
 *
 * 镜头分割页与字幕提取页的进度区是同一套：进度条 + 四个统计项 +「正在处理第 k/N 条」
 * 提示条 + 失败说明 + 输出位置，下面再接一张明细表（明细列由各页面自己给）。
 */

import type { ReactNode } from 'react'
import { Alert, Card, Descriptions, Progress, Space, Tag, Typography } from 'antd'

const { Text } = Typography

/** 三个域的任务状态（三处定义完全一致，这里只取进度卡用得到的那部分） */
export type JobStatus = 'pending' | 'running' | 'success' | 'partial' | 'failed' | 'cancelled'

/** 进度条下方的一个统计项 */
export interface JobStat {
  label: string
  value: ReactNode
}

/** 「正在处理第 k/N 条」提示条的入参 */
export interface CurrentJobHint {
  /** 动词：「处理」/「转写」 */
  label: string
  index: number
  total: number
  name: string
  /** 附在后面的补充（已用时、当前步骤），没有就不显示 */
  extra?: string
}

interface JobProgressCardProps {
  title: ReactNode
  status: JobStatus
  percent: number
  /** 任务是否还在跑（决定进度条是不是滚动的、提示条显不显示） */
  running: boolean
  stats: JobStat[]
  current?: CurrentJobHint | null
  errorMessage?: string
  outputDir?: string
  /** 明细表等附加内容 */
  children?: ReactNode
}

export default function JobProgressCard({
  title,
  status,
  percent,
  running,
  stats,
  current,
  errorMessage,
  outputDir,
  children,
}: JobProgressCardProps) {
  return (
    <Card title={title} style={{ marginBottom: 16 }}>
      <Progress
        percent={percent}
        status={
          status === 'failed'
            ? 'exception'
            : running
              ? 'active'
              : status === 'cancelled'
                ? 'normal'
                : 'success'
        }
      />
      <Descriptions column={{ xs: 1, sm: 2, lg: 4 }} style={{ marginTop: 8 }}>
        {stats.map((stat) => (
          <Descriptions.Item key={stat.label} label={stat.label}>
            {stat.value}
          </Descriptions.Item>
        ))}
      </Descriptions>

      {current && running && (
        <Alert
          type="info"
          showIcon
          style={{ marginTop: 8 }}
          message={
            <span>
              正在{current.label}第 {current.index}/{current.total} 条：
              <Text strong>{current.name}</Text>
              {current.extra && ` · ${current.extra}`}
            </span>
          }
        />
      )}

      {errorMessage && (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 8 }}
          message="部分内容未完成"
          description={<Text style={{ fontSize: 12 }}>{errorMessage}</Text>}
        />
      )}

      {outputDir && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          输出位置：<Text code>{outputDir}</Text>
        </Text>
      )}

      {children}
    </Card>
  )
}

/** 任务标题：`任务 #12` + 状态标签（各页面的进度卡标题都是这个） */
export function JobTitle({
  jobId,
  meta,
  extra,
}: {
  jobId: number
  meta: { label: string; color: string }
  /** 额外的标签（如「预览切点」） */
  extra?: ReactNode
}) {
  return (
    <Space>
      <span>任务 #{jobId}</span>
      <Tag color={meta.color}>{meta.label}</Tag>
      {extra}
    </Space>
  )
}
