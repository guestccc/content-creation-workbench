/**
 * 发布任务调度器。
 *
 * 工作循环：定时向后端认领待执行任务 → 解密本地凭证 → 调用平台适配器发布
 * → 把结果回报给后端。后端只负责排队，真正的发布动作全部发生在客户端，
 * 凭证因此始终不出本机。
 *
 * 并发控制：同时执行的任务数受 concurrency 限制；每条任务独立执行，
 * 单条失败不影响同批次其他任务。
 */

import { BrowserWindow } from 'electron'

import type { PublishTask, SchedulerEvent, SchedulerStatus } from '@shared/types'

import { resolvePublisher } from '../publishers/registry'
import { PublishError } from '../publishers/types'
import { ApiError, api } from './api-client'
import { appConfig } from './app-config'
import { CredentialError, credentialStore } from './credential-store'

/** 发布单条任务的超时时间（毫秒） */
const TASK_TIMEOUT_MS = 120_000

/** 内容缓存有效期（毫秒）：同一内容分发到多个账号时避免重复拉取 */
const CONTENT_CACHE_TTL_MS = 60_000

/** 内容缓存条目 */
interface ContentCacheEntry {
  value: Awaited<ReturnType<typeof api.contents.get>>
  expiresAt: number
}

/** 调度器推送事件的通道名 */
export const SCHEDULER_EVENT_CHANNEL = 'scheduler:event'

class PublishScheduler {
  /** 是否处于运行状态 */
  private running = false

  /** 定时器句柄 */
  private timer: NodeJS.Timeout | null = null

  /** 正在执行的任务 ID */
  private activeTaskIds = new Set<number>()

  /** 是否有一轮轮询正在处理中，避免定时器重入 */
  private ticking = false

  private lastPollAt: string | null = null
  private successCount = 0
  private failedCount = 0
  private lastError = ''

  /** 内容缓存，键为内容 ID */
  private contentCache = new Map<number, ContentCacheEntry>()

  // ------------------------------------------------------------------
  // 生命周期
  // ------------------------------------------------------------------

  /** 启动调度器，若已在运行则直接返回当前状态 */
  start(): SchedulerStatus {
    if (this.running) {
      return this.status()
    }

    this.running = true
    this.lastError = ''
    this.emit({ type: 'started', message: '开始执行发布队列' })

    // 立即跑一轮，避免用户点下按钮后要等一个轮询周期
    void this.tick()

    const intervalMs = appConfig.read().pollIntervalSeconds * 1000
    this.timer = setInterval(() => void this.tick(), intervalMs)

    return this.status()
  }

  /** 停止调度器；已在执行的任务会收到取消信号 */
  stop(): SchedulerStatus {
    if (!this.running) {
      return this.status()
    }

    this.running = false
    if (this.timer) {
      clearInterval(this.timer)
      this.timer = null
    }

    this.emit({
      type: 'stopped',
      message: `已停止执行发布队列（本次运行成功 ${this.successCount} 条，失败 ${this.failedCount} 条）`
    })
    return this.status()
  }

  /** 配置变更后重启定时器，让新的轮询间隔立即生效 */
  applyConfigChange(): void {
    if (!this.running) {
      return
    }
    if (this.timer) {
      clearInterval(this.timer)
    }
    const intervalMs = appConfig.read().pollIntervalSeconds * 1000
    this.timer = setInterval(() => void this.tick(), intervalMs)
  }

  /** 当前运行状态 */
  status(): SchedulerStatus {
    return {
      running: this.running,
      activeTaskIds: [...this.activeTaskIds],
      lastPollAt: this.lastPollAt,
      successCount: this.successCount,
      failedCount: this.failedCount,
      lastError: this.lastError
    }
  }

  // ------------------------------------------------------------------
  // 主循环
  // ------------------------------------------------------------------

  /** 单轮：在并发额度内认领并执行任务 */
  private async tick(): Promise<void> {
    if (!this.running || this.ticking) {
      return
    }

    const config = appConfig.read()
    const freeSlots = config.concurrency - this.activeTaskIds.size
    if (freeSlots <= 0) {
      return
    }

    this.ticking = true
    try {
      const limit = Math.min(freeSlots, config.claimBatchSize)
      const tasks = await api.tasks.claim(limit)
      this.lastPollAt = new Date().toISOString()

      if (tasks.length === 0) {
        this.emit({ type: 'poll', message: '队列为空，等待下一轮' })
        return
      }

      this.emit({ type: 'poll', message: `认领到 ${tasks.length} 条任务` })

      // 并行执行，由 Promise.allSettled 保证单条失败不影响其他任务
      await Promise.allSettled(tasks.map((task) => this.execute(task)))
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      this.lastError = message
      this.emit({ type: 'error', message: `轮询失败：${message}` })
    } finally {
      this.ticking = false
    }
  }

  /** 执行单条任务并回报结果 */
  private async execute(task: PublishTask): Promise<void> {
    this.activeTaskIds.add(task.id)
    this.emit({
      type: 'task-started',
      taskId: task.id,
      message: `开始发布：${task.content_title || `内容 #${task.content_id}`} → ${task.account_nickname}`
    })

    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), TASK_TIMEOUT_MS)

    try {
      const config = appConfig.read()
      const publisher = resolvePublisher(task.platform, config.mockFailureRate)

      // 需要凭证的适配器必须先在本地找到凭证，缺失则直接判失败并提示用户
      const credential = credentialStore.has(task.account_id)
        ? credentialStore.load(task.account_id)
        : null

      if (publisher.requiresCredential && !credential) {
        throw new PublishError(
          `账号「${task.account_nickname}」尚未在本机保存登录凭证，请到「账号与凭证」页补充`,
          false
        )
      }

      const content = await this.fetchContent(task.content_id)

      const result = await publisher.publish(
        {
          taskId: task.id,
          platform: task.platform,
          account: {
            id: task.account_id,
            nickname: task.account_nickname,
            accountUid: ''
          },
          credential,
          content: {
            id: content.id,
            title: content.title,
            body: content.body,
            tags: content.tags
          }
        },
        {
          onProgress: (message) =>
            this.emit({ type: 'task-started', taskId: task.id, message }),
          signal: controller.signal
        }
      )

      // 停止期间被认领的任务：结果照常上报，避免任务永远停在发布中
      const reported = await api.tasks.report(task.id, {
        success: true,
        result_url: result.url
      })

      this.successCount += 1
      this.emit({
        type: 'task-succeeded',
        taskId: task.id,
        message: `发布成功：${reported.content_title} → ${reported.account_nickname}`
      })
    } catch (error) {
      await this.handleFailure(task, error)
    } finally {
      clearTimeout(timer)
      this.activeTaskIds.delete(task.id)
    }
  }

  /** 统一的失败处理：上报失败并区分「已重试」与「已终态」 */
  private async handleFailure(task: PublishTask, error: unknown): Promise<void> {
    const message = this.describeError(error, task)

    try {
      const reported = await api.tasks.report(task.id, {
        success: false,
        error_message: message
      })

      if (reported.status === 'pending') {
        this.emit({
          type: 'task-retried',
          taskId: task.id,
          message: `发布失败，已重新入队（第 ${reported.retry_count} 次重试）：${message}`
        })
        return
      }

      this.failedCount += 1
      this.lastError = message
      this.emit({
        type: 'task-failed',
        taskId: task.id,
        message: `发布失败：${message}`
      })
    } catch (reportError) {
      // 上报本身失败（例如后端不可达），此时任务会停留在发布中，
      // 需要用户在队列页手动重试
      const reportMessage =
        reportError instanceof Error ? reportError.message : String(reportError)
      this.failedCount += 1
      this.lastError = reportMessage
      this.emit({
        type: 'error',
        taskId: task.id,
        message: `结果上报失败（${reportMessage}），任务 ${task.id} 需手动重试`
      })
    }
  }

  /** 把各类异常翻译成面向用户的中文原因 */
  private describeError(error: unknown, task: PublishTask): string {
    if (error instanceof PublishError) {
      return error.message
    }
    if (error instanceof CredentialError) {
      return `账号「${task.account_nickname}」的本地凭证不可用：${error.message}`
    }
    if (error instanceof ApiError) {
      return error.message
    }
    if (error instanceof Error && error.name === 'AbortError') {
      return `发布超时（超过 ${TASK_TIMEOUT_MS / 1000} 秒）`
    }
    return error instanceof Error ? error.message : String(error)
  }

  /** 拉取内容详情，带短期缓存 */
  private async fetchContent(contentId: number) {
    const cached = this.contentCache.get(contentId)
    if (cached && cached.expiresAt > Date.now()) {
      return cached.value
    }

    const content = await api.contents.get(contentId)
    this.contentCache.set(contentId, {
      value: content,
      expiresAt: Date.now() + CONTENT_CACHE_TTL_MS
    })
    return content
  }

  /** 向所有窗口广播事件 */
  private emit(event: Omit<SchedulerEvent, 'at'>): void {
    const payload: SchedulerEvent = { ...event, at: new Date().toISOString() }

    if (event.type === 'error') {
      console.error('[scheduler] %s', payload.message)
    } else {
      console.info('[scheduler] %s', payload.message)
    }

    for (const window of BrowserWindow.getAllWindows()) {
      if (!window.isDestroyed()) {
        window.webContents.send(SCHEDULER_EVENT_CHANNEL, payload)
      }
    }
  }
}

/** 全局唯一的调度器实例 */
export const scheduler = new PublishScheduler()
