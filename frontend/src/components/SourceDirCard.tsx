/**
 * 「选择素材与输出位置」卡片。
 *
 * 镜头分割页与字幕提取页的这一步完全一样：选素材目录 → 列出视频 → 勾选要处理的
 * 几条（不勾=全部）→ 选输出目录。差别只有输出目录的文案，所以文案做成 props。
 *
 * 扫描目录、维护勾选这些逻辑都在 useSourceDir 里，这个组件只负责把它摆出来。
 * 唯一的本地状态是「正在预览哪一条」—— 纯粹是这个卡片自己的开关，
 * 不跨页也不影响任务，没必要往上提。
 */

import { FolderOpenOutlined, PlayCircleOutlined } from '@ant-design/icons'
import { Button, Card, Checkbox, Flex, Space, Switch, Tag, Typography } from 'antd'
import { useState } from 'react'

import { localVideoPreviewUrl } from '../api/filesystem'
import type { UseSourceDirResult } from '../hooks/useSourceDir'
import type { FsEntry } from '../types/scene'
import { formatBytes } from '../utils/format'
import PathBox from './PathBox'
import VideoPreviewModal from './VideoPreviewModal'

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
  /** 正在预览的那一条；null 表示弹窗关着 */
  const [previewing, setPreviewing] = useState<FsEntry | null>(null)

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
                    // 预览按钮放在 Checkbox **外面**：套进 label 里的话，点一下
                    // 预览会连带把这一条勾上/取消勾选 —— 用户只是看一眼，不该
                    // 顺手改了勾选状态
                    <Flex key={entry.name} align="center" gap={8}>
                      <Checkbox value={entry.name} style={{ flex: 1 }}>
                        <Text style={{ fontSize: 13 }}>{entry.name}</Text>
                        <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                          {entry.size_bytes !== null ? formatBytes(entry.size_bytes) : ''}
                        </Text>
                      </Checkbox>
                      <Button
                        type="link"
                        style={{ padding: 0 }}
                        icon={<PlayCircleOutlined />}
                        onClick={() => setPreviewing(entry)}
                      >
                        预览
                      </Button>
                    </Flex>
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

      {/* 素材预览：勾选之前先确认是哪一条素材，省得选错了再白等一遍切分 */}
      <VideoPreviewModal
        open={previewing !== null}
        title={previewing?.name}
        src={previewing ? localVideoPreviewUrl(previewing.path) : undefined}
        // 换一条要换 key，否则浏览器接着放上一条的缓冲
        videoKey={previewing?.path}
        caption={
          <Text type="secondary" style={{ fontSize: 12, wordBreak: 'break-all' }}>
            {previewing?.path}
          </Text>
        }
        width={720}
        onClose={() => setPreviewing(null)}
      />
    </Card>
  )
}
