import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Card, Col, Descriptions, Flex, Row, Statistic, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { Link } from 'react-router-dom'

import { fetchContents, fetchHealth, fetchStatistics } from '../api/contents'
import { describeError } from '../api/client'
import type { Content, ContentStatistics, HealthData } from '../types/content'
import { STATUS_META, STATUS_ORDER } from '../types/content'
import { formatDateTime } from '../utils/format'

const { Title, Text } = Typography

/** 最近更新列表的列定义（不涉及组件状态，放在组件外只建一次） */
const recentColumns: ColumnsType<Content> = [
  { title: '标题', dataIndex: 'title', key: 'title', render: (title: string) => <Text strong>{title}</Text> },
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
    render: (status: Content['status']) => (
      <Tag color={STATUS_META[status].color}>{STATUS_META[status].label}</Tag>
    ),
  },
  {
    title: '更新时间',
    dataIndex: 'updated_at',
    key: 'updated_at',
    render: (value: string) => <Text type="secondary">{formatDateTime(value)}</Text>,
  },
]

/**
 * 工作台概览页。
 *
 * 展示内容统计、后端服务状态与最近更新的内容。
 */
export default function Dashboard() {
  const [stats, setStats] = useState<ContentStatistics | null>(null)
  const [recent, setRecent] = useState<Content[]>([])
  const [health, setHealth] = useState<HealthData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')

    // 三个请求互不依赖，并发发起；用 allSettled 保证单个失败不影响其余数据展示
    const [statsResult, listResult, healthResult] = await Promise.allSettled([
      fetchStatistics(),
      fetchContents({ page: 1, page_size: 5 }),
      fetchHealth(),
    ])

    if (statsResult.status === 'fulfilled') {
      setStats(statsResult.value)
    }
    if (listResult.status === 'fulfilled') {
      setRecent(listResult.value.items)
    }
    if (healthResult.status === 'fulfilled') {
      setHealth(healthResult.value)
    }

    // 统计是首页核心数据，它失败时给出明确提示
    if (statsResult.status === 'rejected') {
      const reason: unknown = statsResult.reason
      setError(describeError(reason, '加载失败，请稍后重试'))
    }

    setLoading(false)
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const databaseOk = health?.database === 'connected'
  const serviceOk = health?.status === 'ok'

  return (
    <Flex vertical gap={22}>
      <Flex justify="space-between" align="flex-start" gap={16}>
        <div>
          <Title level={3} style={{ margin: 0 }}>
            工作台概览
          </Title>
          <Text type="secondary">查看内容创作的整体进展与最近动态</Text>
        </div>
        <Button onClick={() => void load()} loading={loading}>
          刷新
        </Button>
      </Flex>

      {error && <Alert type="error" showIcon message={error} />}

      {/* 顶部统计卡片：总数 + 各状态分布 */}
      <Row gutter={[16, 16]}>
        <Col flex="1 1 160px">
          <Card>
            <Statistic title="内容总数" value={stats ? stats.total : '—'} />
            <div
              style={{
                width: 32,
                height: 3,
                marginTop: 10,
                background: '#2563eb',
                borderRadius: 2,
              }}
            />
          </Card>
        </Col>

        {STATUS_ORDER.map((status) => (
          <Col flex="1 1 160px" key={status}>
            <Card>
              <Statistic title={STATUS_META[status].label} value={stats ? stats.by_status[status] : '—'} />
              <div
                style={{
                  width: 32,
                  height: 3,
                  marginTop: 10,
                  background: STATUS_META[status].color,
                  borderRadius: 2,
                }}
              />
            </Card>
          </Col>
        ))}
      </Row>

      {/* 后端服务状态 */}
      <Card
        title="服务状态"
        extra={
          health && (
            <Tag color={serviceOk ? 'success' : 'warning'}>
              {serviceOk ? '运行正常' : '降级运行'}
            </Tag>
          )
        }
      >
        {health ? (
          <Descriptions
            column={{ xs: 1, sm: 2, md: 3 }}
            items={[
              { key: 'app_name', label: '应用名称', children: health.app_name },
              {
                key: 'version',
                label: '版本',
                children: <Text code>{health.version}</Text>,
              },
              {
                key: 'database',
                label: '数据库',
                children: (
                  <Text style={{ color: databaseOk ? '#16a34a' : '#dc2626' }}>
                    {databaseOk ? '已连接' : '未连接'}
                  </Text>
                ),
              },
            ]}
          />
        ) : (
          <Text type="secondary">
            无法获取服务状态，请确认后端服务已启动（默认 http://127.0.0.1:8000）
          </Text>
        )}
      </Card>

      {/* 最近更新的内容 */}
      <Card
        title="最近更新"
        extra={
          <Link to="/contents">
            <Button >查看全部</Button>
          </Link>
        }
      >
        <Table<Content>
          rowKey="id"
          columns={recentColumns}
          dataSource={recent}
          loading={loading}
          pagination={false}
          locale={{ emptyText: '还没有任何内容，去「内容管理」页创建第一条吧' }}
        />
      </Card>
    </Flex>
  )
}
