/**
 * 轮询一个异步任务，直到它进入终态。
 *
 * 任务由后端异步执行（切一条两分钟素材要几分钟、转写一条视频要几分钟），
 * 前端 fetch 超时只有 15 秒，同步接口必然超时，所以进度只能轮询着拿。
 *
 * 定时器只跟「哪条任务、是否还在跑」绑定：拿到响应写回 state 不会把定时器
 * 拆了重建（重建的话每次响应都会重置计时，间隔就不再是间隔了）。
 */

import { useEffect, useRef } from 'react'

/** 轮询间隔：进度字段每 3 秒落库一次，1.5 秒轮询足够及时又不刷爆后端 */
export const POLL_INTERVAL_MS = 1500

export interface UseJobPollingOptions<J extends { id: number }> {
  /** 要盯的任务；null 表示当前没有任务 */
  job: J | null
  /** 拉一次最新任务（各页面的 fetchXxxJob） */
  fetchJob: (jobId: number) => Promise<J>
  /** 什么算跑完（各域自己的 isTerminalStatus） */
  isTerminal: (job: J) => boolean
  /** 拿到最新任务后写回（一般是 setJob） */
  onUpdate: (job: J) => void
  /** 任务跑到终态时回调一次（拉回产物、刷新历史列表） */
  onFinish?: (job: J) => void
  intervalMs?: number
}

export function useJobPolling<J extends { id: number }>({
  job,
  fetchJob,
  isTerminal,
  onUpdate,
  onFinish,
  intervalMs = POLL_INTERVAL_MS,
}: UseJobPollingOptions<J>): void {
  const latest = useRef({ fetchJob, isTerminal, onUpdate, onFinish })
  useEffect(() => {
    latest.current = { fetchJob, isTerminal, onUpdate, onFinish }
  })

  const jobId = job?.id ?? null
  // 只取「是否在跑」这个布尔值：任务对象每次响应都是新的，拿它当依赖会把定时器拆掉重建
  const running = job !== null && !isTerminal(job)

  useEffect(() => {
    if (jobId === null || !running) {
      return
    }
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const fresh = await latest.current.fetchJob(jobId)
          latest.current.onUpdate(fresh)
          if (latest.current.isTerminal(fresh)) {
            latest.current.onFinish?.(fresh)
          }
        } catch {
          // 单次轮询失败不打断，下个周期会重试
        }
      })()
    }, intervalMs)
    return () => window.clearInterval(timer)
  }, [jobId, running, intervalMs])
}
