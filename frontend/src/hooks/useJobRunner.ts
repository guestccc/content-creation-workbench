/**
 * 页面「当前这条任务」的一整套：创建、轮询、取消、删除、从历史点开。
 *
 * 三个任务型页面（镜头分割 / 字幕提取 / 混剪）在这一块是同构的，差别只在
 * 后端接口和产物形态，所以接口以参数传进来，产物由页面自己管：
 *
 * - 任务跑到终态时回调 onFinished，页面拿它把产物（片段 / 字幕 / 成片）拉回来；
 * - 任务有增删改时回调 onChanged，页面拿它刷新历史列表；
 * - 任务被删除时回调 onRemoved，页面拿它清掉页面上残留的产物与弹窗。
 *
 * 提示文案留给页面：同一个动作在不同页面的说法不一样（「已请求取消」vs
 * 「任务已取消」），只有失败兜底文案是共通的，所以留在 hook 里。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'

import { useJobPolling } from './useJobPolling'

export interface UseJobRunnerOptions<J extends { id: number }, P> {
  /** 创建任务 */
  create: (payload: P) => Promise<J>
  /** 取消任务，返回更新后的任务 */
  cancel: (jobId: number) => Promise<J>
  /** 删除任务记录（是否连磁盘产物一起删，由页面在这个闭包里拼参数） */
  remove: (jobId: number) => Promise<unknown>
  /** 批量删除任务记录（整批成功或整批失败）；不传则 removeMany 不可用 */
  batchRemove?: (jobIds: number[]) => Promise<unknown>
  /** 拉任务详情（轮询与「从历史点开」都用它） */
  fetchJob: (jobId: number) => Promise<J>
  /** 什么算跑完（各域自己的 isTerminalStatus） */
  isTerminal: (job: J) => boolean
  /** 失败提示，传 useApiMessage().fail */
  fail: (error: unknown, fallback: string) => void
  /** 任务新增 / 取消 / 删除 / 跑完 之后刷新历史列表 */
  onChanged?: () => void
  /** 任务被删除（wasCurrent 表示删的正是页面上这条） */
  onRemoved?: (jobId: number, wasCurrent: boolean) => void
  /** 任务跑到终态：页面用它拉回产物 */
  onFinished?: (job: J) => void
}

export interface UseJobRunnerResult<J, P> {
  /** 当前任务；null 表示页面上没有任务 */
  job: J | null
  setJob: Dispatch<SetStateAction<J | null>>
  /** 创建请求进行中（按钮 loading 用） */
  submitting: boolean
  /** 当前任务还在跑（禁用重复提交用） */
  running: boolean
  /** 创建任务；失败时返回 null（提示已发过） */
  submit: (payload: P) => Promise<J | null>
  /** 取消任务；失败时返回 null */
  cancel: (jobId: number) => Promise<J | null>
  /** 删除任务记录；是否成功 */
  remove: (jobId: number) => Promise<boolean>
  /** 批量删除；成功后逐条触发 onRemoved、整体触发一次 onChanged */
  removeMany: (jobIds: number[]) => Promise<boolean>
  /** 读一条任务的详情；失败时返回 null（failMessage 可覆盖兜底文案） */
  read: (jobId: number, failMessage?: string) => Promise<J | null>
}

export function useJobRunner<J extends { id: number }, P>({
  create,
  cancel: cancelJob,
  remove: removeJob,
  batchRemove,
  fetchJob,
  isTerminal,
  fail,
  onChanged,
  onRemoved,
  onFinished,
}: UseJobRunnerOptions<J, P>): UseJobRunnerResult<J, P> {
  const [job, setJob] = useState<J | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const latest = useRef({ create, cancelJob, removeJob, batchRemove, fetchJob, fail, onChanged, onRemoved, onFinished })
  useEffect(() => {
    latest.current = { create, cancelJob, removeJob, batchRemove, fetchJob, fail, onChanged, onRemoved, onFinished }
  })

  // 删除时要判断删的是不是页面上这条：用 ref 读当前值，免得回调把 job 写进依赖
  const jobRef = useRef<J | null>(null)
  useEffect(() => {
    jobRef.current = job
  })

  useJobPolling({
    job,
    fetchJob,
    isTerminal,
    onUpdate: setJob,
    onFinish: (finished) => {
      latest.current.onChanged?.()
      latest.current.onFinished?.(finished)
    },
  })

  const submit = useCallback(async (payload: P): Promise<J | null> => {
    setSubmitting(true)
    try {
      const created = await latest.current.create(payload)
      setJob(created)
      latest.current.onChanged?.()
      return created
    } catch (error) {
      latest.current.fail(error, '创建任务失败')
      return null
    } finally {
      setSubmitting(false)
    }
  }, [])

  const cancel = useCallback(async (jobId: number): Promise<J | null> => {
    try {
      const updated = await latest.current.cancelJob(jobId)
      setJob((current) => (current?.id === jobId ? updated : current))
      latest.current.onChanged?.()
      return updated
    } catch (error) {
      latest.current.fail(error, '取消失败')
      return null
    }
  }, [])

  const remove = useCallback(async (jobId: number): Promise<boolean> => {
    try {
      await latest.current.removeJob(jobId)
      const wasCurrent = jobRef.current?.id === jobId
      if (wasCurrent) {
        setJob(null)
      }
      latest.current.onRemoved?.(jobId, wasCurrent)
      latest.current.onChanged?.()
      return true
    } catch (error) {
      latest.current.fail(error, '删除失败')
      return false
    }
  }, [])

  const removeMany = useCallback(async (jobIds: number[]): Promise<boolean> => {
    const batchRemoveJob = latest.current.batchRemove
    if (!batchRemoveJob || jobIds.length === 0) {
      return false
    }
    try {
      await batchRemoveJob(jobIds)
      const currentId = jobRef.current?.id
      if (currentId !== undefined && jobIds.includes(currentId)) {
        setJob(null)
      }
      // 逐条触发 onRemoved：各页面挂的弹窗清理（详情/产物弹窗）原样复用
      for (const jobId of jobIds) {
        latest.current.onRemoved?.(jobId, jobId === currentId)
      }
      latest.current.onChanged?.()
      return true
    } catch (error) {
      latest.current.fail(error, '批量删除失败')
      return false
    }
  }, [])

  const read = useCallback(
    async (jobId: number, failMessage = '读取任务失败'): Promise<J | null> => {
      try {
        return await latest.current.fetchJob(jobId)
      } catch (error) {
        latest.current.fail(error, failMessage)
        return null
      }
    },
    [],
  )

  return {
    job,
    setJob,
    submitting,
    running: job !== null && !isTerminal(job),
    submit,
    cancel,
    remove,
    removeMany,
    read,
  }
}
