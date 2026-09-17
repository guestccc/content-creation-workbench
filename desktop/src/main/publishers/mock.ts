/**
 * 模拟发布器。
 *
 * 用于在没有接入真实平台接口前跑通整条链路：
 * 认领任务 → 执行 → 上报结果 → 失败重试 → 终态归档。
 *
 * 真实适配器接入后，这里仍作为兜底实现保留：
 * 遇到没有对应适配器的平台时走模拟流程，并明确标注「未接入真实发布」，
 * 避免任务在队列里无声卡死。
 */

import { PublishError, type PublishContext, type PublishRequest, type PublishResult, type Publisher } from './types'

/** 模拟执行的基础耗时（毫秒） */
const BASE_DURATION_MS = 900

/** 模拟执行的耗时抖动上限（毫秒） */
const JITTER_MS = 600

/** 模拟发布器配置 */
export interface MockPublisherOptions {
  /** 平台标识，用于在展示上区分兜底实例与平台专属实例 */
  platform: string
  /** 失败概率（0~1），用于演示失败与重试链路 */
  failureRate: number
}

/** 在指定毫秒后兑现，且响应取消信号 */
function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new PublishError('任务已取消', false))
      return
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    function onAbort(): void {
      clearTimeout(timer)
      reject(new PublishError('任务已取消', false))
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

/** 模拟发布器实现 */
export class MockPublisher implements Publisher {
  readonly id: string
  readonly displayName: string
  readonly platforms: readonly string[]
  readonly requiresCredential = false
  readonly isPlaceholder = true

  private readonly failureRate: number

  constructor(options: MockPublisherOptions) {
    this.platforms = [options.platform]
    this.id = `mock:${options.platform}`
    this.displayName = `${options.platform}（模拟发布）`
    this.failureRate = Math.min(1, Math.max(0, options.failureRate))
  }

  async publish(request: PublishRequest, context: PublishContext): Promise<PublishResult> {
    const duration = BASE_DURATION_MS + Math.floor(Math.random() * JITTER_MS)
    context.onProgress(
      `模拟发布中：${request.content.title || '未命名内容'} → ${request.account.nickname}`
    )

    await delay(duration, context.signal)

    if (Math.random() < this.failureRate) {
      throw new PublishError('模拟发布失败（可在设置中把失败概率调回 0）')
    }

    // 生成一个可点击的占位链接，方便验证「发布成功后回填链接」的链路
    const url = `https://mock.publish.local/${encodeURIComponent(
      request.platform
    )}/${request.account.id}/${request.taskId}`

    context.onProgress('模拟发布完成')
    return { url }
  }
}
