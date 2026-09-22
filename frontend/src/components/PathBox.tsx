/**
 * 路径展示框：占满剩余宽度、过长省略号截断，观感与 antd 输入框一致。
 *
 * 与「选择目录」按钮并排组成 Space.Compact，用在素材目录卡与换背景的原图卡上
 * —— 两处逐字一样，所以抽出来，别各写一份。
 */

import { Typography } from 'antd'

const { Text } = Typography

interface PathBoxProps {
  /** 要显示的绝对路径；空串时显示 placeholder */
  value: string
  placeholder: string
}

export default function PathBox({ value, placeholder }: PathBoxProps) {
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
