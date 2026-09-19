/**
 * 「删除任务时是否连同磁盘上的产物一起删」的勾选状态。
 *
 * 四个任务型页面（镜头分割 / 字幕提取 / 混剪 / 素材抓取）的每个删除确认
 * 框里都带同一个勾选项。三条规则：
 * - 默认不勾 —— 删记录不删文件是安全默认，清产物必须明确勾选；
 * - take() 取走当前值并立刻重置 —— 删文件不可逆，不允许「勾一次管一辈子」；
 * - 确认框打开时调 reset() —— 上一次「勾了又取消」的状态不残留到下一个确认框。
 *
 * 页面用法：checkbox 放进 Popconfirm 的 description；确认回调把 take() 的
 * 结果拼进删除请求（传给 deleteXxxJob / batchDeleteXxxJobs 的 purgeFiles）。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

import { Checkbox } from 'antd'

export interface UsePurgeFilesResult {
  /** 勾选框（渲染进删除确认框的说明区） */
  checkbox: ReactNode
  /** 取走当前勾选值并重置为未勾（确认删除时调用） */
  take: () => boolean
  /** 重置为未勾（删除确认框打开时调用） */
  reset: () => void
}

export function usePurgeFiles(): UsePurgeFilesResult {
  const [checked, setChecked] = useState(false)
  // 删除回调经 useJobRunner 的「最新 ref」转发，拿不到渲染闭包里的 state，
  // 用 ref 同步一份供 take() 读取
  const checkedRef = useRef(false)
  useEffect(() => {
    checkedRef.current = checked
  })

  const take = useCallback((): boolean => {
    const value = checkedRef.current
    setChecked(false)
    return value
  }, [])

  const reset = useCallback(() => setChecked(false), [])

  const checkbox = (
    <Checkbox
      checked={checked}
      onChange={(event) => setChecked(event.target.checked)}
      style={{ marginTop: 4 }}
    >
      同时删除电脑上的任务产物（不可恢复）
    </Checkbox>
  )

  return { checkbox, take, reset }
}
