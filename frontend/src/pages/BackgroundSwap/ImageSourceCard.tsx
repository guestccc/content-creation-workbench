/**
 * 「选择原图与输出位置」卡片（本页私有）。
 *
 * 与公共的 SourceDirCard 长得像，但不能复用：那张卡按视频列，这张按图片列，
 * 而且这里要缩略图预览 —— 换背景的第一步就是「看看是不是这几张手绘图」。
 *
 * 扫描目录、维护勾选都在私有 hook useImageDir 里，这个组件只负责把它摆出来，
 * 因此**没有本地 state**。
 *
 * 缩略图刻意放在 Checkbox **外面**（与 SourceDirCard 的预览按钮同一个理由）：
 * 套进 label 里的话，点一下看图会连带把这一张勾上/取消勾选 —— 用户只是看一眼，
 * 不该顺手改了勾选状态。所有缩略图共用一个 Image.PreviewGroup，点开后能左右翻。
 */

import { PictureOutlined } from '@ant-design/icons'
import { Button, Card, Checkbox, Flex, Image, Space, Typography } from 'antd'

import PathBox from '../../components/PathBox'
import { localImagePreviewUrl } from '../../api/filesystem'
import type { UseImageDirResult } from './useImageDir'
import { formatBytes } from '../../utils/format'

const { Text } = Typography

interface ImageSourceCardProps {
  /** useImageDir 的返回值 */
  dir: UseImageDirResult
  /** 输出目录的绝对路径 */
  outputDir: string
  /** 从别的页面带图进来时的说明；没有就说不出「这批图哪来的」 */
  prefillNotice?: string
  /** 打开目录选择器自己挑一个目录 */
  onPickInput: () => void
  /** 打开「从素材抓取选图」弹窗挑一条笔记的图 */
  onPickFromCrawl: () => void
  onPickOutput: () => void
}

export default function ImageSourceCard({
  dir,
  outputDir,
  prefillNotice,
  onPickInput,
  onPickFromCrawl,
  onPickOutput,
}: ImageSourceCardProps) {
  const { path, data, images, selected } = dir

  return (
    <Card
      title={
        <Space size={8}>
          <PictureOutlined style={{ color: 'var(--color-primary)' }} />
          选择原图与输出位置
        </Space>
      }
      style={{ height: '100%' }}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <div>
          <Text type="secondary">原图目录</Text>
          <Space.Compact style={{ width: '100%', marginTop: 4 }}>
            <Button onClick={onPickInput}>选择目录</Button>
            <PathBox value={path} placeholder="尚未选择" />
          </Space.Compact>
          {/* 抓取产物落在 <输出目录>/<平台>/images/<笔记 id>/ 这种分层目录里，
              目录选择器不递归，手动点进去看不到图 —— 所以给一条专门的入口 */}
          <Button type="link" style={{ paddingLeft: 0 }} onClick={onPickFromCrawl}>
            从素材抓取选图
          </Button>
          {prefillNotice && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {prefillNotice}
            </Text>
          )}
        </div>

        {data && (
          <div>
            <Flex justify="space-between" align="center" style={{ marginBottom: 6 }}>
              <Text type="secondary">
                可处理图片 {images.length} 张
                {selected.length > 0 && ` · 已勾选 ${selected.length} 张`}
              </Text>
              <Space size={4}>
                <Button onClick={dir.rescan} disabled={!path}>
                  重新扫描
                </Button>
                <Checkbox
                  checked={selected.length === 0}
                  onChange={(event) => dir.toggleAll(event.target.checked)}
                >
                  未勾选=全部
                </Checkbox>
              </Space>
            </Flex>
            <div
              style={{
                maxHeight: 260,
                overflow: 'auto',
                border: '1px solid var(--color-border)',
                borderRadius: 8,
                padding: '8px 12px',
              }}
            >
              {images.length === 0 ? (
                <Text type="warning">该目录下没有可处理的图片文件</Text>
              ) : (
                // lazy 加载：一目录几百张大图时，不能一进页面就发几百个请求
                <Image.PreviewGroup>
                  <Checkbox.Group
                    value={selected}
                    onChange={(values) => dir.setSelected(values as string[])}
                    style={{ display: 'flex', flexDirection: 'column', gap: 6 }}
                  >
                    {images.map((entry) => (
                      <Flex key={entry.name} align="center" gap={8}>
                        <Image
                          src={localImagePreviewUrl(entry.path)}
                          width={36}
                          height={36}
                          loading="lazy"
                          style={{ objectFit: 'cover', borderRadius: 4 }}
                        />
                        <Checkbox value={entry.name} style={{ flex: 1 }}>
                          <Text style={{ fontSize: 13 }}>{entry.name}</Text>
                          <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                            {entry.size_bytes !== null ? formatBytes(entry.size_bytes) : ''}
                          </Text>
                        </Checkbox>
                      </Flex>
                    ))}
                  </Checkbox.Group>
                </Image.PreviewGroup>
              )}
            </div>
          </div>
        )}

        <div>
          <Text type="secondary">输出目录（换完背景的图放这里）</Text>
          <Space.Compact style={{ width: '100%', marginTop: 4 }}>
            <Button onClick={onPickOutput}>选择目录</Button>
            <PathBox value={outputDir} placeholder="默认（materials/background）" />
          </Space.Compact>
          <Text type="secondary" style={{ fontSize: 12 }}>
            每次任务生成一个 background-&lt;时间戳&gt; 子目录，产物同名 .png；删任务时可连这个目录一起清掉
          </Text>
        </div>
      </Space>
    </Card>
  )
}
