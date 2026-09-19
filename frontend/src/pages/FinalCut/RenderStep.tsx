/**
 * 第 ③ 步：框选与合成。
 *
 * 左边是 BoxSelector（视频上画框/拖框/缩放，框内实时预览排版）；右边面板
 * 依次是：正在定位的候选切换（带完成标记）、样式三选一色块、字号自动/手动、
 * 四个百分比微调输入、「应用到全部文案」、「开始合成」。
 *
 * 合成中显示 JobProgressCard（第 k/N 条）；跑完把成片摆成卡片网格
 * （封面 + 播放角标 → 预览弹窗）。本组件只持有「当前在调哪条候选」「正在
 * 预览哪条成片」这类纯 UI 状态，任务与候选状态都在 useFinalcutFlow。
 */

import { useEffect, useMemo, useState } from 'react'
import {
  ArrowLeftOutlined,
  CheckCircleFilled,
  PlayCircleOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Flex,
  InputNumber,
  Row,
  Segmented,
  Slider,
  Tag,
  Tooltip,
  Typography,
} from 'antd'

import JobProgressCard from '../../components/JobProgressCard'
import VideoPreviewModal from '../../components/VideoPreviewModal'
import { formatBytes, formatDuration } from '../../utils/format'
import { ITEM_STATUS_META, isTerminalStatus } from '../../types/finalcut'
import type {
  FinalcutRenderItem,
  FinalcutTextStyleItem,
} from '../../types/finalcut'
import BoxSelector from './BoxSelector'
import type { UseFinalcutFlowResult } from './useFinalcutFlow'

const { Text } = Typography

/** 环境自检没给到样式表时的兜底（与后端 TEXT_STYLES 同配色） */
const FALLBACK_STYLES: FinalcutTextStyleItem[] = [
  {
    key: 'white_box',
    label: '白字黑边带底',
    preview_text: '#ffffff',
    preview_background: 'rgba(0, 0, 0, 0.45)',
    default: true,
  },
  {
    key: 'yellow',
    label: '黄字黑边',
    preview_text: '#ffe95c',
    preview_background: 'rgba(0, 0, 0, 0.25)',
    default: false,
  },
  {
    key: 'outline',
    label: '无底纯描边',
    preview_text: '#ffffff',
    preview_background: 'rgba(0, 0, 0, 0)',
    default: false,
  },
]

interface RenderStepProps {
  flow: UseFinalcutFlowResult
  /** 视频预览流地址（页面已按来源算好） */
  videoUrl: string
  videoKey?: string
  /** 环境自检下发的样式表（色块与框内预览共用配色） */
  styles?: FinalcutTextStyleItem[]
}

/** 百分比微调输入（x/y/w/h 通用）：显示整数 %，提交时除回 0-1 */
function PercentInput({
  label,
  value,
  onChange,
}: {
  label: string
  value: number
  onChange: (next: number) => void
}) {
  return (
    <Flex align="center" gap={4}>
      <Text type="secondary" style={{ fontSize: 12, width: 14 }}>
        {label}
      </Text>
      <InputNumber
        size="small"
        min={0}
        max={100}
        value={Math.round(value * 100)}
        onChange={(next) => {
          if (typeof next === 'number') {
            onChange(next / 100)
          }
        }}
        style={{ width: 64 }}
      />
    </Flex>
  )
}

export default function RenderStep({ flow, videoUrl, videoKey, styles }: RenderStepProps) {
  const styleItems = styles && styles.length > 0 ? styles : FALLBACK_STYLES

  // 正在定位的候选：默认第一条勾选的；勾选集合变化时校准
  const checked = useMemo(() => flow.candidates.filter((item) => item.checked), [flow.candidates])
  const [activeKey, setActiveKey] = useState<number | null>(checked[0]?.key ?? null)
  useEffect(() => {
    if (activeKey === null || !checked.some((item) => item.key === activeKey)) {
      setActiveKey(checked[0]?.key ?? null)
    }
  }, [checked, activeKey])
  const active = checked.find((item) => item.key === activeKey) ?? null

  const [previewItem, setPreviewItem] = useState<FinalcutRenderItem | null>(null)

  const renderJob = flow.renderJob
  const renderRunning = Boolean(renderJob && !isTerminalStatus(renderJob.status))
  const currentItem = renderJob?.items.find((item) => item.index === renderJob.current_index)

  const activeStyleMeta =
    styleItems.find((item) => item.key === (active?.style ?? 'white_box')) ?? styleItems[0]

  /** 百分比微调：改一个维度，其余保持 */
  const patchBox = (patch: Partial<{ x: number; y: number; w: number; h: number }>) => {
    if (!active) {
      return
    }
    const next = { ...active.box, ...patch }
    // 微调也保证不出画面（框选手势由 moveable 钳边界，数字输入要自己钳）
    next.x = Math.min(next.x, 1 - next.w)
    next.y = Math.min(next.y, 1 - next.h)
    flow.updateCandidateBox(active.key, next)
  }

  return (
    <Flex vertical gap={16}>
      <Flex justify="space-between" align="center">
        <Button icon={<ArrowLeftOutlined />} onClick={flow.goBack}>
          返回文案
        </Button>
        <Text type="secondary">
          已框选 {checked.filter((item) => item.boxSet).length}/{checked.length} 条
          （未框选的用默认位置：画面下方居中）
        </Text>
      </Flex>

      <Row gutter={16}>
        <Col xs={24} lg={14}>
          {active ? (
            <BoxSelector
              src={videoUrl}
              videoKey={videoKey}
              box={active.box}
              onBoxChange={(box) => flow.updateCandidateBox(active.key, box)}
              text={active.text}
              fontSize={active.fontSize}
              previewStyle={{
                textColor: activeStyleMeta.preview_text,
                background: activeStyleMeta.preview_background,
              }}
            />
          ) : (
            <Empty description="没有勾选的文案，回上一步勾选" />
          )}
        </Col>
        <Col xs={24} lg={10}>
          <Flex vertical gap={12}>
            <Card size="small" title="正在定位的文案">
              <Flex vertical gap={4}>
                {checked.map((item) => (
                  <Flex
                    key={item.key}
                    align="center"
                    gap={8}
                    style={{
                      cursor: 'pointer',
                      padding: '4px 6px',
                      borderRadius: 6,
                      background:
                        item.key === activeKey ? 'var(--ant-color-primary-bg)' : undefined,
                    }}
                    onClick={() => setActiveKey(item.key)}
                  >
                    {item.boxSet ? (
                      <CheckCircleFilled style={{ color: 'var(--ant-color-success)' }} />
                    ) : (
                      <span style={{ width: 14, display: 'inline-block' }} />
                    )}
                    <Text ellipsis style={{ flex: 1, fontSize: 13 }}>
                      {item.text}
                    </Text>
                  </Flex>
                ))}
              </Flex>
            </Card>

            {active && (
              <Card size="small" title="样式与字号">
                <Flex vertical gap={12}>
                  <Flex gap={8}>
                    {styleItems.map((item) => (
                      <Tooltip key={item.key} title={item.label}>
                        <div
                          onClick={() => flow.updateCandidateStyle(active.key, item.key)}
                          style={{
                            cursor: 'pointer',
                            padding: '6px 12px',
                            borderRadius: 6,
                            border:
                              active.style === item.key
                                ? '2px solid var(--ant-color-primary)'
                                : '1px solid var(--ant-color-border)',
                            background: item.preview_background || 'rgba(0,0,0,0.06)',
                            color: item.preview_text,
                            textShadow: '0 0 2px #000, 1px 1px 1px #000',
                            fontSize: 13,
                            fontWeight: 600,
                          }}
                        >
                          文案
                        </div>
                      </Tooltip>
                    ))}
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {activeStyleMeta.label}
                    </Text>
                  </Flex>

                  <Flex align="center" gap={12}>
                    <Segmented
                      value={active.fontSize > 0 ? '手动' : '自动'}
                      onChange={(value) =>
                        flow.updateCandidateFontSize(active.key, value === '手动' ? 40 : 0)
                      }
                      options={['自动', '手动']}
                    />
                    {active.fontSize > 0 && (
                      <Slider
                        style={{ flex: 1 }}
                        min={16}
                        max={64}
                        value={active.fontSize}
                        onChange={(value) =>
                          flow.updateCandidateFontSize(active.key, value as number)
                        }
                      />
                    )}
                  </Flex>

                  <Flex gap={8} wrap>
                    <PercentInput label="x" value={active.box.x} onChange={(v) => patchBox({ x: v })} />
                    <PercentInput label="y" value={active.box.y} onChange={(v) => patchBox({ y: v })} />
                    <PercentInput label="宽" value={active.box.w} onChange={(v) => patchBox({ w: v })} />
                    <PercentInput label="高" value={active.box.h} onChange={(v) => patchBox({ h: v })} />
                  </Flex>

                  <Button
                    size="small"
                    onClick={() => flow.applyStyleToAll(active.style, active.fontSize)}
                  >
                    把这条的样式与字号应用到全部文案
                  </Button>
                </Flex>
              </Card>
            )}
          </Flex>
        </Col>
      </Row>

      <Flex justify="center">
        <Button
          type="primary"
          size="large"
          icon={<ThunderboltOutlined />}
          disabled={checked.length === 0 || renderRunning}
          loading={flow.renderJobSubmitting}
          onClick={() => void flow.startRenderJob()}
        >
          开始合成（{checked.length} 条成片）
        </Button>
      </Flex>

      {renderJob && !isTerminalStatus(renderJob.status) && (
        <JobProgressCard
          title={`合成任务 #${renderJob.id}`}
          status={renderJob.status}
          percent={renderJob.progress_percent}
          running={renderRunning}
          stats={[
            { label: '成片数', value: renderJob.total_items },
            { label: '已完成', value: renderJob.completed_items },
            { label: '失败', value: renderJob.failed_items },
          ]}
          current={
            currentItem
              ? {
                  label: '合成',
                  index: renderJob.current_index,
                  total: renderJob.total_items,
                  name:
                    currentItem.copy_text.length > 24
                      ? `${currentItem.copy_text.slice(0, 24)}…`
                      : currentItem.copy_text,
                }
              : null
          }
        />
      )}

      {renderJob && isTerminalStatus(renderJob.status) && (
        <>
          {renderJob.status === 'failed' && (
            <Alert type="error" showIcon message="合成失败" description={renderJob.error_message} />
          )}
          {renderJob.status === 'cancelled' && (
            <Alert type="info" showIcon message="合成任务已取消，已产出的成片保留" />
          )}
          <Card title={`成片（任务 #${renderJob.id}）`}>
            {renderJob.items.length === 0 ? (
              <Empty description="没有成片明细" />
            ) : (
              <Row gutter={[12, 12]}>
                {renderJob.items.map((item) => (
                  <Col xs={12} sm={8} md={6} key={item.id}>
                    <Card
                      size="small"
                      styles={{ body: { padding: 8 } }}
                      cover={
                        item.status === 'success' && item.thumb_url ? (
                          <div
                            style={{ position: 'relative', cursor: 'pointer' }}
                            onClick={() => setPreviewItem(item)}
                          >
                            <img
                              src={item.thumb_url}
                              alt={item.output_name}
                              style={{ width: '100%', display: 'block', borderRadius: '6px 6px 0 0' }}
                            />
                            <PlayCircleOutlined
                              style={{
                                position: 'absolute',
                                inset: 0,
                                margin: 'auto',
                                width: 32,
                                height: 32,
                                fontSize: 32,
                                color: 'rgba(255,255,255,0.9)',
                              }}
                            />
                          </div>
                        ) : undefined
                      }
                    >
                      <Flex vertical gap={4}>
                        <Flex align="center" gap={4}>
                          <Tag color={ITEM_STATUS_META[item.status].color}>
                            {ITEM_STATUS_META[item.status].label}
                          </Tag>
                          <Text style={{ fontSize: 12 }}>#{item.index}</Text>
                        </Flex>
                        <Tooltip title={item.copy_text}>
                          <Text ellipsis style={{ fontSize: 12 }}>
                            {item.copy_text}
                          </Text>
                        </Tooltip>
                        {item.status === 'success' ? (
                          <Text type="secondary" style={{ fontSize: 11 }}>
                            {formatDuration(item.duration_seconds)} · {formatBytes(item.size_bytes)}
                            {item.resolved_font_size > 0 && ` · ${item.resolved_font_size}px`}
                          </Text>
                        ) : (
                          <Text type="danger" style={{ fontSize: 11 }}>
                            {item.error_message || '未产出'}
                          </Text>
                        )}
                      </Flex>
                    </Card>
                  </Col>
                ))}
              </Row>
            )}
          </Card>
        </>
      )}

      <VideoPreviewModal
        open={previewItem !== null}
        title={previewItem ? `第 ${previewItem.index} 条成片` : ''}
        src={previewItem?.video_url}
        videoKey={previewItem?.id}
        caption={previewItem?.output_path}
        onClose={() => setPreviewItem(null)}
      />
    </Flex>
  )
}
