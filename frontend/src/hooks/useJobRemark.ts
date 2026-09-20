/**
 * 任务备注的编辑状态。
 *
 * 六个历史任务列表的判断完全一样：点了哪一行的备注列 → 弹窗打开 → 保存成功
 * 就关弹窗、发提示、刷新列表。所以开关状态放这里，各页面只管把弹窗挂上。
 *
 * 弹窗里点的是列表行本身（备注就在行上），编辑中的那条任务原样存着即可，
 * 不用按 id 再去取一次详情。
 *
 * 只收 message 实例、不收整个 useApiMessage 的返回值：失败提示在弹窗内展示
 * （见 JobRemarkModal），这里只用得上成功提示。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import type { UseApiMessageResult } from './useApiMessage'

export interface UseJobRemarkOptions {
  /** useApiMessage 的 message 实例：只用来发「保存成功」的提示 */
  message: UseApiMessageResult['message']
  /** 保存成功后刷新列表，传 history.reload */
  onSaved: () => void
  /** 成功提示文案，默认「备注已保存」 */
  successText?: string
}

export interface UseJobRemarkResult<J extends { id: number }> {
  /** 正在编辑备注的任务；null 表示弹窗关着 */
  editing: J | null
  /** 点某行的备注列：打开这条任务的备注编辑弹窗 */
  open: (job: J) => void
  /** 关闭弹窗（未保存） */
  close: () => void
  /** 弹窗保存成功：关弹窗 + 提示 + 刷新列表 */
  handleSaved: () => void
}

export function useJobRemark<J extends { id: number }>({
  message,
  onSaved,
  successText = '备注已保存',
}: UseJobRemarkOptions): UseJobRemarkResult<J> {
  const [editing, setEditing] = useState<J | null>(null)

  // 「最新 ref」模式：message / onSaved 都是页面每次渲染新建的，进 ref 后
  // open / close / handleSaved 的引用恒定，页面不用把它们写进依赖数组
  const latest = useRef({ message, onSaved, successText })
  useEffect(() => {
    latest.current = { message, onSaved, successText }
  })

  const open = useCallback((job: J) => setEditing(job), [])
  const close = useCallback(() => setEditing(null), [])

  const handleSaved = useCallback(() => {
    setEditing(null)
    latest.current.message.success(latest.current.successText)
    // 刷新是静默失败的（useJobList 的既有约定）：失败时单元格还是旧备注，不再补提示
    latest.current.onSaved()
  }, [])

  return { editing, open, close, handleSaved }
}
