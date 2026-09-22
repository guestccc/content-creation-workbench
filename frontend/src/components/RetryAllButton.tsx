/**
 * 「重试全部未完成的」按钮。
 *
 * 四个有条目状态机的任务页（换背景 / 字幕提取 / 智能混剪 / 镜头分割）共用：把这条
 * 任务里所有**失败与跳过**的条目一起重新入队，已成功的条目结果原样不动。
 *
 * 计数为 0 时整个按钮不渲染 —— 没有可重试的东西就不该有个按钮杵在那儿。
 *
 * ⚠️ 批量必须走一条请求（后端的 `/jobs/{id}/retry`），**不能在前端循环调单条**：
 * 第一次调用就把任务置回 pending，第二次会撞上「任务尚未结束」的终态校验 409，
 * 循环会半途而废（这条有接口测试钉着）。
 *
 * 点击后由调用方的 onRetry 负责调接口与刷新（各页面的状态源不同：当前任务、
 * 历史弹窗、列表各有一份）。
 */

import { Button } from 'antd'
import { RedoOutlined } from '@ant-design/icons'

export default function RetryAllButton({
  count,
  onRetry,
  disabled = false,
}: {
  /** 可重试的条目数（failed + skipped），为 0 时不渲染 */
  count: number
  onRetry: () => void
  /** 正在跑（或正在发请求）时置灰 */
  disabled?: boolean
}) {
  if (count <= 0) {
    return null
  }
  return (
    <Button icon={<RedoOutlined />} disabled={disabled} onClick={onRetry}>
      重试全部未完成的（{count}）
    </Button>
  )
}
