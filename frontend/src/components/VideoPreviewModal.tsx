/**
 * 视频预览弹窗。
 *
 * 片段、素材、成片都用它播放：原生 <video> + 关闭即卸载。
 * 关掉必须卸载 <video>，否则弹窗关了后台还在下载、还在出声。
 */

import type { ReactNode } from 'react'
import { Flex, Modal } from 'antd'

interface VideoPreviewModalProps {
  open: boolean
  title?: ReactNode
  /** 视频流地址（后端支持 Range，可拖进度条） */
  src?: string
  /**
   * 切换视频时要变的 key（一般传片段序号 / 素材 id）：
   * 不换 key 的话浏览器会接着放上一条的缓冲
   */
  videoKey?: string | number
  /** 底部内容（如「上一个 / 下一个」）；不传表示不显示底栏 */
  footer?: ReactNode
  /** 视频下方的补充信息（如素材的绝对路径） */
  caption?: ReactNode
  width?: number
  onClose: () => void
}

export default function VideoPreviewModal({
  open,
  title,
  src,
  videoKey,
  footer,
  caption,
  width = 480,
  onClose,
}: VideoPreviewModalProps) {
  return (
    <Modal
      open={open}
      title={title}
      footer={footer ?? null}
      width={width}
      onCancel={onClose}
      destroyOnHidden
    >
      {src && (
        <Flex vertical gap={8}>
          <video
            key={videoKey}
            src={src}
            controls
            autoPlay
            style={{ width: '100%', maxHeight: '70vh', background: '#000', borderRadius: 6 }}
          />
          {caption}
        </Flex>
      )}
    </Modal>
  )
}
