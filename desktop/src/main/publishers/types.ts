/**
 * 发布器适配器接口。
 *
 * 设计意图：把「平台差异」全部收敛到 Publisher 实现里。
 * 调度器只认识这个接口，新增一个平台 = 新增一个适配器文件 + 注册一行，
 * 调度器、IPC、界面都不需要改动。
 *
 * 凭证只在调用 publish 时以参数形式传入，适配器不得将其写入日志或持久化。
 */

import type { CredentialPayload } from '@shared/types'

/** 一次发布请求的上下文 */
export interface PublishRequest {
  /** 发布任务 ID，用于幂等与埋点 */
  taskId: number
  /** 目标平台名称（原始中文名，如「抖音」） */
  platform: string
  /** 账号信息（不含凭证） */
  account: {
    id: number
    nickname: string
    accountUid: string
  }
  /** 已解密的凭证；适配器声明 requiresCredential 时必定非空 */
  credential: CredentialPayload | null
  /** 待发布内容 */
  content: {
    id: number
    title: string
    body: string
    tags: string[]
  }
}

/** 发布结果 */
export interface PublishResult {
  /** 发布成功后的内容链接 */
  url: string
}

/** 执行过程中的回调，用于向界面反馈进度 */
export interface PublishContext {
  /** 上报一条进度文字，会展示在客户端的运行日志中 */
  onProgress(message: string): void
  /** 超时 / 取消信号，适配器应尽快响应 */
  signal: AbortSignal
}

/** 发布失败时抛出的异常，携带面向用户的错误原因 */
export class PublishError extends Error {
  /** 是否属于「重试可能成功」的临时性错误 */
  readonly retryable: boolean

  constructor(message: string, retryable = true) {
    super(message)
    this.name = 'PublishError'
    this.retryable = retryable
  }
}

/** 平台发布器 */
export interface Publisher {
  /** 适配器唯一标识 */
  readonly id: string
  /** 适配器展示名 */
  readonly displayName: string
  /** 该适配器负责的平台名称列表 */
  readonly platforms: readonly string[]
  /** 是否需要登录凭证 */
  readonly requiresCredential: boolean
  /** 是否为占位实现（尚未接入真实平台接口） */
  readonly isPlaceholder: boolean

  /**
   * 执行一次发布。
   *
   * @throws PublishError 发布失败
   */
  publish(request: PublishRequest, context: PublishContext): Promise<PublishResult>
}
