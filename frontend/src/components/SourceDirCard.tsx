/**
 * 「选择素材与输出位置」卡片。
 *
 * 镜头分割页与字幕提取页的这一步完全一样：选素材目录 → 列出视频 → 勾选要处理的
 * 几条（不勾=全部）→ 选输出目录。差别只有输出目录的文案，所以文案做成 props。
 *
 * 扫描目录、维护勾选这些逻辑都在 useSourceDir 里，这个组件只负责把它摆出来。
 */

import { FolderOpenOutlined } from '@ant-design/icons'
import { Button, Card, Checkbox, Flex, Space, Switch, Tag, Typography } from 'antd'

import type { UseSourceDirResult } from '../hooks/useSourceDir'
import { formatBytes } from '../utils/format'

const { Text } = Typography

interface SourceDirCardProps {
  /** useSourceDir 的返回值 */
  dir: UseSourceDirResult
  /** 输出目录的绝对路径 */
  outputDir: string
  /** 输出目录那一行的标签 */
  outputLabel: string
  /** 输出目录为空时的占位文案 */
  outputPlaceholder: string
  /** 输出目录下方的说明 */
  outputHint: string
  /** 当前看的正是素材根目录（materials/source）—— 空列表时的提示要分情况说 */
  isEmptySourceDir: boolean
  onPickInput: () => void
  onPickOutput: () => void
}

/** 路径展示框：占满剩余宽度，过长省略（与 antd 输入框同样的内边距与边框） */
function PathBox({ value, placeholder }: { value: string; placeholder: string }) {
  return (
    <Text
      style={{
        flex: 1,
        padding: '4px 11px',
        border: '1px solid var(--color-border)',
        borderRadius: 6,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        color: value ? undefined : 'var(--color-text-muted)',
      }}
    >
      {value || placeholder}
    </Text>
  )
}

export default function SourceDirCard({
  dir,
  outputDir,
  outputLabel,
  outputPlaceholder,
  outputHint,
  isEmptySourceDir,
  onPickInput,
  onPickOutput,
}: SourceDirCardProps) {
  const { path, data, videos, selected } = dir

  return (
    <Card
      title={
        <Space size={8}>
          <FolderOpenOutlined style={{ color: 'var(--color-primary)' }} />
          选择素材与输出位置
        </Space>
      }
      style={{ height: '100%' }}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <div>
          <Text type="secondary">素材目录</Text>
          <Space.Compact style={{ width: '100%', marginTop: 4 }}>
            <Button onClick={onPickInput}>选择目录</Button>
            <PathBox value={path} placeholder="尚未选择" />
          </Space.Compact>
        </div>

        {data && (
          <div>
            <Flex justify="space-between" align="center" style={{ marginBottom: 6 }}>
              <Text type="secondary">
                可处理视频 {videos.length} 条
                {selected.length > 0 && ` · 已勾选 ${selected.length} 条`}
              </Text>
              <Space size={4}>
                <Button onClick={dir.rescan} disabled={!path}>
                  重新扫描
                </Button>
                <Checkbox checked={selected.length === 0} onChange={(event) => dir.toggleAll(event.target.checked)}>
                  未勾选=全部
                </Checkbox>
                <Tag color="blue">递归子目录</Tag>
                <Switch checked={dir.recursive} onChange={dir.setRecursive} />
              </Space>
            </Flex>
            <div
              style={{
                maxHeight: 168,
                overflow: 'auto',
                border: '1px solid var(--color-border)',
                borderRadius: 8,
                padding: '8px 12px',
              }}
            >
              {videos.length === 0 ? (
                isEmptySourceDir ? (
                  // source/ 第一次用的时候必然是空的，这里直接说清「往哪儿放」
                  <Text type="warning">
                    还没有素材 —— 把视频拷进 {data.path}，再点「重新扫描」
                  </Text>
                ) : (
                  <Text type="warning">该目录下没有可处理的视频文件</Text>
                )
              ) : (
                <Checkbox.Group
                  value={selected}
                  onChange={(values) => dir.setSelected(values as string[])}
                  style={{ display: 'flex', flexDirection: 'column', gap: 6 }}
                >
                  {videos.map((entry) => (
                    <Checkbox key={entry.name} value={entry.name}>
                      <Text style={{ fontSize: 13 }}>{entry.name}</Text>
                      <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                        {entry.size_bytes !== null ? formatBytes(entry.size_bytes) : ''}
                      </Text>
                    </Checkbox>
                  ))}
                </Checkbox.Group>
              )}
            </div>
          </div>
        )}

        <div>
          <Text type="secondary">{outputLabel}</Text>
          <Space.Compact style={{ width: '100%', marginTop: 4 }}>
            <Button onClick={onPickOutput}>选择目录</Button>
            <PathBox value={outputDir} placeholder={outputPlaceholder} />
          </Space.Compact>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {outputHint}
          </Text>
        </div>
      </Space>
    </Card>
  )
}
