/**
 * 统一的接口提示。
 *
 * 页面里的 catch 分支一律写成 `fail(error, '创建任务失败')`：
 * ApiError 用后端返回的原文，其他异常才用兜底文案。这样页面里不再出现
 * 到处复制的 `error instanceof ApiError ? error.message : '...'`。
 *
 * message 实例由这个 hook 持有，页面把返回的 contextHolder 渲染到自己的根节点上
 * （antd 的 message 需要挂到当前 React 树上才能拿到 ConfigProvider 的主题与语言）。
 */

import { useCallback } from 'react'
import type { ReactNode } from 'react'
import { message } from 'antd'

import { describeError } from '../api/client'

export interface UseApiMessageResult {
  /** antd 的 message 实例：页面自己发提示（「已复制」这类）时用它 */
  message: ReturnType<typeof message.useMessage>[0]
  /** 接口失败：ApiError 用后端文案，其余用兜底文案 */
  fail: (error: unknown, fallback: string) => void
  /** 必须渲染进页面，否则 message 拿不到主题与语言 */
  contextHolder: ReactNode
}

export function useApiMessage(): UseApiMessageResult {
  const [messageApi, contextHolder] = message.useMessage()

  const fail = useCallback(
    (error: unknown, fallback: string) => {
      messageApi.error(describeError(error, fallback))
    },
    [messageApi],
  )

  return { message: messageApi, fail, contextHolder }
}
