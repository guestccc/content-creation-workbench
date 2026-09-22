/**
 * 字幕内容预览弹窗（字幕提取与一键成品共用）。
 *
 * 只认「任务 id + 字幕序号」，**不认路径** —— 后端的预览接口就是这么设计的：
 * 文件路径完全由任务记录推导，让前端传路径等于开一个任意文件读取的口子。
 * 因此本地 .srt 文件走不了这个弹窗，调用方要自己判断手头有没有这两项
 * （一键成品那边只有「从历史产物选择」来的字幕才有）。
 *
 * 拉取与视图状态都在组件内部：调用方只管给 target、关窗时给 null，页面保持
 * 「只做编排」。图省事把 fetch 留在各页面里，两处预览就会慢慢长得不一样。
 */

import { useEffect, useMemo, useState } from 'react'
import { Button, Flex, Modal, Segmented, Space, Spin, Typography } from 'antd'
import { CopyOutlined, FileTextOutlined } from '@ant-design/icons'

import { fetchSubtitleText } from '../api/subtitle'
import { useApiMessage } from '../hooks'
import type { SubtitleText } from '../types/subtitle'
import { formatBytes } from '../utils/format'

const { Text } = Typography

/** 弹窗里右侧的视图：srt 原文 / 去掉时间轴的纯文本 */
type PreviewView = 'srt' | 'plain'

/** 预览目标：任务 id + 字幕序号（任务内从 1 开始，与后端接口的 index 同义） */
export interface SubtitlePreviewTarget {
  jobId: number
  index: number
}

interface SubtitlePreviewModalProps {
  /** 要预览的字幕；null 表示关闭（组件据此弹/收） */
  target: SubtitlePreviewTarget | null
  onClose: () => void
}

/**
 * srt 原文 → 去掉序号与时间轴的纯文本：一个字幕块一行，块与块之间空一行，
 * 块内的多行字幕（双语、长句折行）照旧换行。
 *
 * 纯展示用的文本变换，不落盘、不改后端产物；文件本身还是原来的 .srt。
 */
function srtToPlainText(content: string): string {
  const blocks: string[] = []
  for (const block of content.split(/\r?\n[ \t]*\r?\n/)) {
    const lines = block.split(/\r?\n/)
    // 块首的纯数字是字幕序号，去掉；没有序号的文件不多走这一步
    if (lines.length > 0 && /^\s*\d+\s*$/.test(lines[0])) {
      lines.shift()
    }
    const text = lines
      .filter((line) => !SRT_TIME_LINE.test(line))
      .join('\n')
      .trim()
    if (text) {
      blocks.push(text)
    }
  }
  return blocks.join('\n\n')
}

/**
 * 一行 srt 时间轴。逗号是标准写法，但有的工具（含部分 ASR 导出）用小数点，
 * 箭头也可能写成 `->`，一并认掉；行首行尾的空白不敏感。
 */
const SRT_TIME_LINE = /^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-+>\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*$/

export default function SubtitlePreviewModal({
  target,
  onClose,
}: SubtitlePreviewModalProps) {
  const { message, fail, contextHolder } = useApiMessage()
  const [preview, setPreview] = useState<SubtitleText | null>(null)
  const [loading, setLoading] = useState(false)
  const [view, setView] = useState<PreviewView>('srt')

  useEffect(() => {
    if (!target) {
      return
    }
    // 换一条字幕就把上一条的内容与视图收掉：留着旧内容会让人以为新的一条
    // 已经加载好了（视图也一律从 srt 原文看起，不把上一条的选择带过来）
    setPreview(null)
    setView('srt')
    setLoading(true)

    // 关窗/换条时旧请求仍可能返回，用标志位丢掉它 —— 否则慢的那条会盖掉新的
    let cancelled = false
    fetchSubtitleText(target.jobId, target.index)
      .then((data) => {
        if (!cancelled) {
          setPreview(data)
        }
      })
      .catch((error) => {
        if (!cancelled) {
          fail(error, '读取字幕失败')
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [target, fail])

  /** 无时间文本按当前预览内容现算：不二次请求，切换视图是瞬时的 */
  const plainText = useMemo(
    () => (preview ? srtToPlainText(preview.content) : ''),
    [preview],
  )
  /** 弹窗里当前显示的内容（复制按钮复制的也是它） */
  const content = view === 'srt' ? (preview?.content ?? '') : plainText

  const copy = () => {
    void navigator.clipboard.writeText(content).then(
      () => message.success('已复制'),
      () => message.error('复制失败，请手动选择复制'),
    )
  }

  return (
    <>
      {contextHolder}
      <Modal
        open={target !== null}
        title={
          preview && (
            // 文件名可能很长（如抓取素材的哈希名）：一行放不下，挤在一行里
            // 会导致它和来源名各截一半，主要信息反而认不出来，所以分两行 ——
            // 首行文件名（跟图标同行），次行小字来源，各自单行省略号截断、
            // 悬浮看全名；右侧留出关闭按钮的宽度，别让省略号顶到 × 上
            <Flex vertical gap={2} style={{ minWidth: 0, paddingRight: 32 }}>
              <Flex align="center" gap={8} style={{ minWidth: 0 }}>
                <FileTextOutlined style={{ flexShrink: 0 }} />
                <Text ellipsis={{ tooltip: preview.name }} style={{ minWidth: 0 }}>
                  {preview.name}
                </Text>
              </Flex>
              <Text
                type="secondary"
                ellipsis={{ tooltip: `来自 ${preview.source_name}` }}
                // 24 = 图标 16 + 间距 8，与首行文件名的左边缘对齐
                style={{
                  minWidth: 0,
                  fontWeight: 'normal',
                  fontSize: 12,
                  paddingInlineStart: 24,
                }}
              >
                来自 {preview.source_name}
              </Text>
            </Flex>
          )
        }
        // 视图切换与复制都在正文上方的工具条里，不放底栏：
        // 内容是长文，操作跟在内容下面时要先滚到底才看得见
        footer={null}
        width={720}
        onCancel={onClose}
        destroyOnHidden
      >
        {loading && !preview ? (
          <Flex justify="center" style={{ padding: 32 }}>
            <Spin />
          </Flex>
        ) : (
          preview && (
            <Flex vertical gap={8}>
              <Flex justify="space-between" align="center" gap={12}>
                <Space size={8}>
                  <Segmented
                    value={view}
                    onChange={(value) => setView(value as PreviewView)}
                    options={[
                      { label: 'srt 原文', value: 'srt' },
                      { label: '无时间文本', value: 'plain' },
                    ]}
                  />
                  <Button icon={<CopyOutlined />} onClick={copy}>
                    复制{view === 'srt' ? '全文' : '纯文本'}
                  </Button>
                </Space>
                <Text type="secondary" ellipsis style={{ minWidth: 0, fontSize: 12 }}>
                  {formatBytes(preview.size_bytes)}
                  {preview.truncated && ' · 内容过长，只显示了开头一段'}
                </Text>
              </Flex>
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
                {content}
              </pre>
            </Flex>
          )
        )}
      </Modal>
    </>
  )
}
