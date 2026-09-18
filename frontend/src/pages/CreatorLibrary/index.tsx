/**
 * 创作者主页管理页（图文二创组）。
 *
 * 按平台收藏常抓的创作者，素材抓取的「创作者主页」模式可以直接从库里选人。
 * 数据量小：全量拉一次（useCreatorLibrary），平台用 Tabs 分、标签用 Select 筛，
 * 都在前端做。
 */

import { useState } from 'react'
import { Alert, Button, Card, Flex, Modal, Select, Space, Table, Tabs, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'

import CreatorFormModal from './CreatorFormModal'
import { useCreatorLibrary } from './useCreatorLibrary'
import { useApiMessage } from '../../hooks'
import { PLATFORM_META, PLATFORM_ORDER } from '../../types/crawler'
import type { CrawlPlatform } from '../../types/crawler'
import type { Creator } from '../../types/creator'
import { formatDateTime } from '../../utils/format'

const { Title, Text } = Typography

export default function CreatorLibrary() {
  // 纯展示的本地状态：当前平台 tab 与标签筛选（切平台时标签跟着清空）
  const [activePlatform, setActivePlatform] = useState<CrawlPlatform>('xhs')
  const [tagFilter, setTagFilter] = useState<string | undefined>(undefined)

  // antd 的 message / Modal.confirm 需要挂到当前 React 树上（useXxx 形式），
  // 直接用静态方法会拿不到 ConfigProvider 的主题与语言
  const api = useApiMessage()
  const [confirmApi, confirmContext] = Modal.useModal()
  const library = useCreatorLibrary(api)

  /** 删除（带二次确认；确认框的 loading 由 onOk 返回的 Promise 驱动） */
  const handleDelete = (creator: Creator) => {
    void confirmApi.confirm({
      title: `确定要删除「${creator.name}」吗？`,
      content: '只删除库里的记录，历史抓取任务不受影响。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => library.remove(creator),
    })
  }

  const columns: ColumnsType<Creator> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      render: (name: string) => <Text strong>{name}</Text>,
    },
    {
      title: '主页链接 / ID',
      dataIndex: 'homepage',
      key: 'homepage',
      render: (homepage: string) => (
        // copyable 的复制内容用原始值，展示时不截断（链接长了表格自己横向滚）
        <Text copyable={{ text: homepage }}>{homepage}</Text>
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
      title: '备注',
      dataIndex: 'remark',
      key: 'remark',
      render: (remark: string) => remark || <Text type="secondary">—</Text>,
    },
    {
      title: '添加时间',
      dataIndex: 'created_at',
      key: 'created_at',
      render: (value: string) => <Text type="secondary">{formatDateTime(value)}</Text>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 130,
      render: (_, creator) => (
        <Space size={0}>
          <Button type="link" onClick={() => library.openEdit(creator)}>
            编辑
          </Button>
          <Button type="link" danger onClick={() => handleDelete(creator)}>
            删除
          </Button>
        </Space>
      ),
    },
  ]

  /** 单个平台 tab 的内容：标签筛选 + 计数 + 列表（纯展示片段，数据已按平台分好） */
  const renderPane = (platformCreators: Creator[]) => {
    const tagOptions = [...new Set(platformCreators.flatMap((creator) => creator.tags))].map(
      (tag) => ({ value: tag }),
    )
    const filtered = tagFilter
      ? platformCreators.filter((creator) => creator.tags.includes(tagFilter))
      : platformCreators

    return (
      <div>
        <Flex justify="space-between" align="middle" gap={12} wrap style={{ marginBottom: 16 }}>
          <Space size={12} wrap>
            <Select
              allowClear
              placeholder="按标签筛选"
              style={{ minWidth: 160 }}
              value={tagFilter}
              onChange={(value?: string) => setTagFilter(value)}
              options={tagOptions}
              disabled={platformCreators.length === 0}
            />
            <Text type="secondary">{filtered.length} 位创作者</Text>
          </Space>
        </Flex>

        <Table<Creator>
          rowKey="id"
          columns={columns}
          dataSource={filtered}
          loading={library.loading}
          pagination={false}
          locale={{
            emptyText:
              platformCreators.length === 0
                ? '该平台还没有收藏的创作者，点右上角「添加创作者」开始'
                : '没有打这个标签的创作者',
          }}
        />
      </div>
    )
  }

  const tabItems = PLATFORM_ORDER.map((platform) => {
    const platformCreators = library.creators.filter((creator) => creator.platform === platform)
    return {
      key: platform,
      label: `${PLATFORM_META[platform].label} ${platformCreators.length}`,
      children: renderPane(platformCreators),
    }
  })

  return (
    <Flex vertical gap={22}>
      {api.contextHolder}
      {confirmContext}

      <Flex justify="space-between" align="flex-start" gap={16}>
        <div>
          <Title level={3} style={{ margin: 0 }}>
            创作者主页
          </Title>
          <Text type="secondary">
            按平台收藏常抓的创作者；素材抓取的「创作者主页」模式可直接从库里选人
          </Text>
        </div>
        <Button type="primary" onClick={library.openCreate}>
          + 添加创作者
        </Button>
      </Flex>

      {library.error && <Alert type="error" showIcon message={library.error} />}

      <Card>
        <Tabs
          items={tabItems}
          activeKey={activePlatform}
          onChange={(key) => {
            // 换平台后旧标签大概率不存在，直接清掉，避免空列表
            setActivePlatform(key as CrawlPlatform)
            setTagFilter(undefined)
          }}
        />
      </Card>

      {library.modal && (
        <CreatorFormModal
          editing={library.modal.editing}
          defaultPlatform={activePlatform}
          onClose={library.closeModal}
          onSaved={library.handleSaved}
        />
      )}
    </Flex>
  )
}
