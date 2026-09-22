/**
 * 第 ② 步左列：当前文案任务用的那份字幕（本页私有，只有这个页面用）。
 *
 * 右边是 AI 写的文案，左边放它读到的素材 —— 这样才看得出「AI 是没读懂素材，
 * 还是读懂了但写得差」。两个视图都由后端给：
 * - 「字幕原文」= 磁盘上那份产物（带序号与时间轴）；
 * - 「AI 输入的素材」= 去掉序号/时间轴、合并连续重复行、剥掉 ASS 标签，
 *   并按字数上限截断后的那份 —— 前端自己写的文本变换做不到这些，别拿它冒充。
 *
 * 任务状态不影响这里显示：排队中、失败、取消时也照常给（失败时恰恰最需要看
 * AI 读到了什么），所以没有「只在成功后渲染」之类的条件。
 */

import { useState } from 'react'
import { Alert, Button, Card, Flex, Segmented, Space, Spin, Typography } from 'antd'
import { CopyOutlined, FileTextOutlined } from '@ant-design/icons'

import { useApiMessage } from '../../hooks'
import type { FinalcutCopySubtitleText } from '../../types/finalcut'
import { formatBytes } from '../../utils/format'

const { Text } = Typography

/** 卡片里的两种视图：磁盘上的原文 / 喂给 AI 的素材 */
type MaterialView = 'content' | 'material'

interface SubtitleMaterialCardProps {
  /** 字幕内容；还没取到或取失败时为 null */
  data: FinalcutCopySubtitleText | null
  loading: boolean
  /** 取字幕失败的原因（空串 = 正常） */
  error: string
}

export default function SubtitleMaterialCard({
  data,
  loading,
  error,
}: SubtitleMaterialCardProps) {
  const { message, contextHolder } = useApiMessage()
  const [view, setView] = useState<MaterialView>('content')

  /** 当前视图的文本（复制按钮复制的也是它） */
  const text = data ? (view === 'content' ? data.content : data.material) : ''

  const copy = () => {
    void navigator.clipboard.writeText(text).then(
      () => message.success('已复制'),
      () => message.error('复制失败，请手动选择复制'),
    )
  }

  return (
    <Card
      title={
        // 文件名可能很长：一行放不下，让它自己省略号截断、悬浮看全名
        <Flex align="center" gap={8} style={{ minWidth: 0 }}>
          <FileTextOutlined style={{ flexShrink: 0 }} />
          <Text ellipsis={{ tooltip: data?.name }} style={{ minWidth: 0 }}>
            {data?.name ?? '原始字幕'}
          </Text>
          {data && (
            <Text type="secondary" style={{ fontSize: 12, fontWeight: 'normal' }}>
              {formatBytes(data.size_bytes)}
            </Text>
          )}
        </Flex>
      }
      extra={
        <Button icon={<CopyOutlined />} size="small" disabled={!text} onClick={copy}>
          复制
        </Button>
      }
    >
      {contextHolder}

      {error ? (
        // 字幕读不到不该妨碍看文案：只在卡内说明，页面上其余部分照常
        <Alert
          type="warning"
          showIcon
          message="读不到这份字幕"
          description={error}
        />
      ) : loading && !data ? (
        <Flex justify="center" style={{ padding: 32 }}>
          <Spin />
        </Flex>
      ) : (
        data && (
          <Flex vertical gap={8}>
            <Space size={8} wrap>
              <Segmented
                value={view}
                size="small"
                onChange={(value) => setView(value as MaterialView)}
                options={[
                  { label: '字幕原文', value: 'content' },
                  { label: 'AI 输入的素材', value: 'material' },
                ]}
              />
              <Text code ellipsis={{ tooltip: data.path }} style={{ maxWidth: 200, fontSize: 12 }}>
                {data.path}
              </Text>
            </Space>

            <pre
              style={{
                maxHeight: '60vh',
                overflow: 'auto',
                padding: 12,
                background: 'var(--color-bg-soft, #f5f5f5)',
                borderRadius: 6,
                fontSize: 12,
                lineHeight: 1.7,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-all',
                margin: 0,
              }}
            >
              {text}
            </pre>

            {/* 两个截断是两回事，别合成一句话：前者是「页面上只显示了开头」，
                后者是「AI 当时也只读到了这些」 */}
            {view === 'content' && data.truncated && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                文件较大，只显示了开头一段
              </Text>
            )}
            {view === 'material' && data.material_truncated && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                素材超过字数上限，AI 当时也只读到了这些
              </Text>
            )}
          </Flex>
        )
      )}
    </Card>
  )
}
