/**
 * 渲染进程侧的接口封装。
 *
 * 主进程返回的是 { ok, data | error } 结构，这里统一拆包：
 * 成功返回数据，失败抛出 AppError，让页面可以用 try/catch 处理，
 * 同时也让 React 的 setState 逻辑保持线性。
 */

import type { IpcResult } from '@shared/types'

/** 客户端统一错误类型 */
export class AppError extends Error {
  readonly code: string

  constructor(message: string, code = 'UNKNOWN') {
    super(message)
    this.name = 'AppError'
    this.code = code
  }
}

/**
 * 拆解 IPC 返回结果。
 *
 * @throws AppError 主进程返回失败时
 */
export async function call<T>(promise: Promise<IpcResult<T>>): Promise<T> {
  let result: IpcResult<T>
  try {
    result = await promise
  } catch (error) {
    // 主进程崩溃或通道异常时走到这里
    throw new AppError(
      `客户端内部通信失败：${error instanceof Error ? error.message : String(error)}`,
      'IPC_ERROR'
    )
  }

  if (!result.ok) {
    throw new AppError(result.error.message, result.error.code)
  }
  return result.data
}

/** 把任意异常转成可展示的文案 */
export function toMessage(error: unknown): string {
  if (error instanceof AppError) {
    return error.message
  }
  return error instanceof Error ? error.message : String(error)
}
