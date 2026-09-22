/**
 * 一条候选文案的正文（第 ② 步右列，一个候选一个 Tab 面板）。
 *
 * 组成：角度标签 + 实时字数/秒数行 + 复制按钮 + 可编辑的文案正文 + 三段折叠说明
 * （为什么这么写 / 好在哪里 / 逐段拆解）。
 *
 * 「实时」是这里的重点：正文是可编辑的 textarea，用户删掉两句就该立刻看到
 * 「约念几秒」掉下去 —— 这条文案是要拿去剪映配音、念满整个视频时长的，秒数
 * 够不够只有边改边看才判断得了。字数与秒数用 `copyLength.ts` 的镜像函数算，
 * 口径与后端一致（含标点、不含换行）。
 *
 * 外框（卡片边框、Tab 栏）由页面上的 Tabs 给，这里不再套一层 Card。
 * 纯展示组件：状态在 useFinalcutFlow 的 candidates 里，这里只把改动抛回去。
 */

import { Button, Collapse, Flex, Input, List, Table, Tag, Typography } from 'antd'

import { useApiMessage } from '../../hooks'
import type { CopyCandidate } from '../../types/finalcut'
import { countCopyChars, estimateSeconds } from './copyLength'
import type { CandidateState } from './useFinalcutFlow'

const { Text } = Typography

interface CopyCardProps {
  /** AI 产物原文（角度 / 为什么 / 好在哪 / 拆解都来自它） */
  candidate: CopyCandidate
  /** 页面侧状态（可能被改过的正文） */
  state: CandidateState
  onTextChange: (text: string) => void
  /** 口播语速（字/秒）；0 = 还没探测到，只显示字数不给秒数 */
  charsPerSecond: number
  /** 字数预算 [下限, 上限]；null = 缺时长或语速，只显示字数 */
  budget: [number, number] | null
}

/** 字数相对预算的落点：差多少 / 超多少 / 正好在区间里 */
function budgetHint(count: number, budget: [number, number] | null): string {
  if (!budget) {
    return ''
  }
  const [low, high] = budget
  if (count < low) {
    return `还差 ${low - count} 字`
  }
  if (count > high) {
    return `超出 ${count - high} 字`
  }
  return '已够（在预算内）'
}

export default function CopyCard({
  candidate,
  state,
  onTextChange,
  charsPerSecond,
  budget,
}: CopyCardProps) {
  // 复制成功/失败的提示走 useApiMessage：项目里没有 <App> 包裹，用静态 message 会丢主题
  const { message, contextHolder } = useApiMessage()

  const count = countCopyChars(state.text)
  const seconds = estimateSeconds(state.text, charsPerSecond)
  const hint = budgetHint(count, budget)

  const copy = () => {
    void navigator.clipboard.writeText(state.text).then(
      () => message.success('已复制，去剪映粘贴即可'),
      () => message.error('复制失败，请手动选择复制'),
    )
  }

  return (
    <Flex vertical>
      {contextHolder}
      <Flex align="center" gap={8} wrap style={{ marginBottom: 8 }}>
        {candidate.angle && <Tag>{candidate.angle}</Tag>}
        {charsPerSecond > 0 && (
          <Text strong style={{ fontSize: 12 }}>
            约念 {Math.round(seconds)} 秒
          </Text>
        )}
        <Text type="secondary" style={{ fontSize: 12 }}>
          {count} 字
          {budget ? `（预算 ${budget[0]}–${budget[1]}）` : ''}
          {hint ? ` · ${hint}` : ''}
        </Text>
        <Button size="small" onClick={copy}>
          复制文案
        </Button>
      </Flex>

      <Input.TextArea
        value={state.text}
        onChange={(event) => onTextChange(event.target.value)}
        autoSize={{ minRows: 4, maxRows: 14 }}
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
    </Flex>
  )
}
