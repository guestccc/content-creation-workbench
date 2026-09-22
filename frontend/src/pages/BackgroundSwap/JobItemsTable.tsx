/**
 * 产物明细表：每张原图一行，带缩略图、尺寸、耗时、诊断提示，展开行是完整统计。
 *
 * 换背景页有两处要摆它 —— 当前任务的进度卡片里、历史任务「查看」的弹窗里 ——
 * 所以单独一个文件，而不是把这段 JSX 抄两遍。只有本页用得到，按项目约定留在
 * 页面自己的文件夹里，不进 src/components/。
 *
 * 这张表也是本页的产物清单（每张原图对应一张 PNG），不另开一份产物列表。
 */

import type { ReactNode } from 'react'
import { Button, Flex, Image, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'

import { backgroundOutputUrl } from '../../api/background'
import RetryAllButton from '../../components/RetryAllButton'
import { ITEM_STATUS_META, isTerminalStatus } from '../../types/background'
import type { BackgroundJob, BackgroundJobItem, BackgroundStats } from '../../types/background'
import { formatBytes, formatElapsed } from '../../utils/format'

const { Text } = Typography

export default function JobItemsTable({
  job,
  onRetryItem,
  onRetryAll,
}: {
  job: BackgroundJob
  /** 重试某一张（失败 / 跳过的行才有这个按钮） */
  onRetryItem?: (item: BackgroundJobItem) => void
  /** 重试这条任务里所有失败 / 跳过的图片 */
  onRetryAll?: (job: BackgroundJob) => void
}) {
  if (job.items.length === 0) {
    return null
  }
  return (
    <Flex vertical gap={8}>
      {/* 「重试全部」摆在表头位置：这张表同时挂在进度卡与历史弹窗里，
          按钮放这儿两处自动都有。没跑完时不给点（后端也会以 409 挡住） */}
      {onRetryAll !== undefined && isTerminalStatus(job.status) && (
        <Flex justify="flex-end">
          <RetryAllButton
            count={job.failed_images + job.skipped_images}
            onRetry={() => onRetryAll(job)}
          />
        </Flex>
      )}
      <Table
        rowKey="id"
        pagination={false}
        dataSource={job.items}
        columns={itemColumns(job, onRetryItem)}
        expandable={{
          // 诊断数字默认收起来：它是排查用的，平时只占地方。
          // 失败的行没有统计；有 warnings 的行默认展开，那正是要看的时候
          defaultExpandedRowKeys: job.items
            .filter((item) => (item.stats.warnings ?? []).length > 0)
            .map((item) => item.id),
          expandedRowRender: (item) => <StatsBlock stats={item.stats} />,
          rowExpandable: (item) => Object.keys(item.stats).length > 0,
        }}
      />
    </Flex>
  )
}

/** 每张原图的处理明细列定义 */
function itemColumns(
  job: BackgroundJob,
  onRetryItem?: (item: BackgroundJobItem) => void,
): ColumnsType<BackgroundJobItem> {
  const columns: ColumnsType<BackgroundJobItem> = [
    { title: '#', dataIndex: 'index', width: 48 },
    { title: '原图', dataIndex: 'source_name', ellipsis: true },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (status: keyof typeof ITEM_STATUS_META) => (
        <Tag color={ITEM_STATUS_META[status].color}>{ITEM_STATUS_META[status].label}</Tag>
      ),
    },
    {
      title: '产物',
      width: 88,
      render: (_: unknown, item: BackgroundJobItem) =>
        item.status === 'success' ? (
          // 所有缩略图共用一个 PreviewGroup：点开后能左右翻着看这一批产物
          <Image.PreviewGroup>
            <Image
              src={backgroundOutputUrl(job.id, item.index)}
              width={56}
              height={56}
              loading="lazy"
              style={{ objectFit: 'contain', background: 'var(--color-bg-soft, #f5f5f5)', borderRadius: 4 }}
            />
          </Image.PreviewGroup>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: '尺寸',
      width: 104,
      render: (_: unknown, item: BackgroundJobItem) =>
        item.width > 0 ? `${item.width}×${item.height}` : '—',
    },
    {
      title: '大小',
      width: 88,
      render: (_: unknown, item: BackgroundJobItem) =>
        item.size_bytes > 0 ? formatBytes(item.size_bytes) : '—',
    },
    {
      title: '耗时',
      dataIndex: 'elapsed_seconds',
      width: 80,
      render: (value: number, item: BackgroundJobItem) => {
        // 正在跑的这一张显示实时已用时：抠图没有中间进度可上报
        if (item.status === 'running' && item.index === job.current_index) {
          return (
            <Text style={{ fontSize: 12 }}>{formatElapsed(job.current_elapsed_seconds)}</Text>
          )
        }
        return value > 0 ? `${value}s` : '—'
      },
    },
    {
      title: '诊断',
      width: 150,
      render: (_: unknown, item: BackgroundJobItem) => (
        <WarningsCell stats={item.stats} />
      ),
    },
    {
      title: '说明',
      dataIndex: 'error_message',
      ellipsis: true,
      render: (value: string) =>
        value ? (
          <Tooltip title={value}>
            <Text type={value === '任务已取消' ? 'secondary' : 'danger'} style={{ fontSize: 12 }}>
              {value}
            </Text>
          </Tooltip>
        ) : (
          '—'
        ),
    },
  ]

  // 操作列只在调用方给了 onRetryItem 时才出现（两个挂载点都给了）
  if (onRetryItem !== undefined) {
    columns.push({
      title: '操作',
      width: 72,
      render: (_: unknown, item: BackgroundJobItem) => {
        // 可重试 = 「没有产出」：failed 是跑了但失败，skipped 是被取消 / 服务重启
        // 时压根没轮到。两者都该给用户一个补跑的机会，而不是整条任务重来。
        const retryable =
          (item.status === 'failed' || item.status === 'skipped') &&
          isTerminalStatus(job.status)
        if (!retryable) {
          return <Text type="secondary">—</Text>
        }
        return (
          <Button type="link" style={{ padding: 0 }} onClick={() => onRetryItem(item)}>
            重试
          </Button>
        )
      },
    })
  }

  return columns
}

/** 诊断列的单元格：有 warnings 就顶出来，没有就看一眼可见占比 */
function WarningsCell({ stats }: { stats: BackgroundStats }) {
  const warnings = stats.warnings ?? []
  if (warnings.length > 0) {
    return (
      <Tooltip title={warnings.join('；')}>
        <Tag color="warning" style={{ marginInlineEnd: 0 }}>
          {warnings.length} 条提示
        </Tag>
      </Tooltip>
    )
  }
  if (typeof stats.visible_ratio !== 'number') {
    return <Text type="secondary">—</Text>
  }
  return (
    <Text type="secondary" style={{ fontSize: 12 }}>
      可见 {(stats.visible_ratio * 100).toFixed(2)}%
    </Text>
  )
}

/**
 * 展开行里的诊断明细。
 *
 * 这个算法的失败模式（定位线把背景圈进来、整张蒙雾）光看产物图判不出来，
 * 这组数字才是排查依据，所以在每一行下面直接摊开。
 */
function StatsBlock({ stats }: { stats: BackgroundStats }) {
  const rows: { label: string; value: ReactNode }[] = [
    {
      label: '纸 / 墨亮度',
      value: `${numberText(stats.paper)} / ${numberText(stats.ink_luma)}（跨度 ${numberText(stats.span)}）`,
    },
    { label: '定位线', value: stats.dark_line || '—' },
    {
      label: '位置门',
      value: stats.gate ? `开，扩边 ${numberText(stats.gate_pad)} px` : '关',
    },
    {
      label: '暖色抑制',
      value: stats.warm ? `开，抠掉 ${numberText(stats.warm_cut)} px` : '关',
    },
    {
      label: '墨色',
      value: Array.isArray(stats.ink_rgb) ? stats.ink_rgb.join(', ') : '—',
    },
    {
      label: '贴纸 / 背景尺寸',
      value: `${sizeText(stats.sticker)} → ${sizeText(stats.page)}`,
    },
    // 贴纸那一项是**裁边缩放之前**的画布尺寸，实际贴上去的是「× 缩放比例」之后的大小
    // —— 少了这一行，默认 0.1 之下会看着像 bug（一个 300×200 的贴纸贴进 400×400 的
    // 背景，图上明明只有一点点大）
    { label: '缩放', value: scaleText(stats.scale) },
    {
      label: '落点',
      value: Array.isArray(stats.position) ? stats.position.join(', ') : '—',
    },
    {
      label: '不透明 / 可见像素',
      value: `${numberText(stats.opaque_px)} / ${numberText(stats.visible_px)}`,
    },
    { label: '输入已透明', value: stats.already_transparent ? '是（未重新抠）' : '否' },
  ]
  const warnings = stats.warnings ?? []

  return (
    <Flex vertical gap={8}>
      {warnings.length > 0 && (
        <Space wrap size={4}>
          {warnings.map((warning) => (
            <Tag key={warning} color="warning" style={{ marginInlineEnd: 0 }}>
              {warning}
            </Tag>
          ))}
        </Space>
      )}
      <Flex vertical gap={4}>
        {rows.map((row) => (
          <Text key={row.label} style={{ fontSize: 12 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {row.label}：
            </Text>
            {row.value}
          </Text>
        ))}
      </Flex>
    </Flex>
  )
}

/** 数字展示：拿不到就给占位符，别显示成 undefined */
function numberText(value: unknown): string {
  if (typeof value !== 'number' || Number.isNaN(value)) {
    return '—'
  }
  return Number.isInteger(value) ? String(value) : value.toFixed(2)
}

/** 缩放比例展示：贴纸宽 = 背景宽 × 这个数；拿不到（null）= 原尺寸贴 */
function scaleText(value: unknown): string {
  if (typeof value !== 'number' || Number.isNaN(value)) {
    return '不缩放（原尺寸）'
  }
  return `${value}（贴纸宽 = 背景宽 × ${value}）`
}

/** [宽, 高] 形式的尺寸展示 */
function sizeText(value: unknown): string {
  if (!Array.isArray(value) || value.length < 2) {
    return '—'
  }
  return `${value[0]}×${value[1]}`
}
