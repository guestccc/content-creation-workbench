/**
 * 一条候选广告文案的卡片。
 *
 * 组成：勾选框 + 可编辑的文案正文（实时字数）+ 角度/目标秒数标签 +
 * 三段折叠说明（为什么这么写 / 好在哪里 / 逐段拆解）。
 *
 * 纯展示组件：状态在 useFinalcutFlow 的 candidates 里，这里只把改动抛回去。
 */

import { Card, Checkbox, Collapse, Flex, Input, List, Table, Tag, Typography } from 'antd'

import type { CopyCandidate } from '../../types/finalcut'
import type { CandidateState } from './useFinalcutFlow'

const { Text } = Typography

interface CopyCardProps {
  /** AI 产物原文（角度 / 为什么 / 好在哪 / 拆解都来自它） */
  candidate: CopyCandidate
  /** 页面侧状态（勾选与否、改过的字） */
  state: CandidateState
  onToggle: () => void
  onTextChange: (text: string) => void
}

export default function CopyCard({ candidate, state, onToggle, onTextChange }: CopyCardProps) {
  return (
    <Card
      size="small"
      style={{
        borderColor: state.checked ? 'var(--ant-color-primary)' : undefined,
      }}
      title={
        <Flex align="center" gap={8} wrap>
          <Checkbox checked={state.checked} onChange={onToggle} />
          {candidate.angle && <Tag color="geekblue">{candidate.angle}</Tag>}
          {candidate.target_seconds > 0 && (
            <Tag>目标 {candidate.target_seconds}s</Tag>
          )}
          <Text type="secondary" style={{ fontSize: 12 }}>
            {state.text.length} 字
          </Text>
        </Flex>
      }
    >
      <Input.TextArea
        value={state.text}
        onChange={(event) => onTextChange(event.target.value)}
        autoSize={{ minRows: 2, maxRows: 6 }}
        style={{ marginBottom: 8 }}
      />
      <Collapse
        ghost
        size="small"
        items={[
          {
            key: 'why',
            label: '为什么这么写',
            children: <Text>{candidate.why || '（AI 未给出说明）'}</Text>,
          },
          {
            key: 'highlights',
            label: `好在哪里（${candidate.highlights.length} 条）`,
            children: (
              <List
                size="small"
                dataSource={candidate.highlights}
                renderItem={(item) => <List.Item style={{ padding: '4px 0' }}>· {item}</List.Item>}
              />
            ),
          },
          {
            key: 'breakdown',
            label: `逐段拆解（${candidate.breakdown.length} 段）`,
            children: (
              <Table
                size="small"
                rowKey={(row) => row.part}
                pagination={false}
                dataSource={candidate.breakdown}
                columns={[
                  { title: '段落', dataIndex: 'part', width: 110 },
                  { title: '原文', dataIndex: 'content' },
                  { title: '作用', dataIndex: 'explain' },
                ]}
              />
            ),
          },
        ]}
      />
    </Card>
  )
}
