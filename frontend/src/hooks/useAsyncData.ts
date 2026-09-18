/**
 * 进页面时拉一份数据（环境自检、模板清单、素材库都用它）。
 *
 * 只做「拉取 → 存起来 → 失败提示」这一件事，不含任何业务判断：
 * 拿到数据之后要做的事（回填默认目录、切换当前素材目录）通过 onLoaded 交回页面。
 *
 * `load` 可以带一个入参，用于「重新检测」这类需要换个参数重跑的场合；
 * 入参由调用方在 reload 时显式传入，而不是让 load 去读 state —— 否则
 * setState 之后立刻 reload 会读到还没更新的旧闭包。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { describeError } from '../api/client'

export interface UseAsyncDataOptions<T, A> {
  /** 拉取数据；A 是重跑时可以换的参数（不需要就留空） */
  load: (arg: A) => Promise<T>
  /** 失败时的兜底文案，同时也是 error 状态的内容 */
  failMessage: string
  /** 失败时只记 error 状态、不弹提示（环境自检这类「不阻断页面」的场景） */
  silent?: boolean
  /** 提示接口，直接传 useApiMessage().fail */
  fail?: (error: unknown, fallback: string) => void
  /** 拿到数据后要做的事 */
  onLoaded?: (data: T) => void
}

export interface UseAsyncDataResult<T, A> {
  data: T | null
  loading: boolean
  /** 失败原因（成功时为空串） */
  error: string
  /** 重新拉取；传入的参数会交给 load */
  reload: (arg?: A) => Promise<T | null>
  /**
   * 直接替换数据。
   *
   * 给「写接口返回的就是最新数据」的场合用：改完直接塞回来，省掉一次
   * 多余的 GET（也避免中间那一帧的空窗）。
   */
  setData: (data: T) => void
}

export function useAsyncData<T, A = undefined>({
  load,
  failMessage,
  silent = false,
  fail,
  onLoaded,
}: UseAsyncDataOptions<T, A>): UseAsyncDataResult<T, A> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // 回调与 load 都放进 ref：reload 的引用保持稳定，调用方不必把它们写进依赖数组
  const latest = useRef({ load, fail, onLoaded })
  useEffect(() => {
    latest.current = { load, fail, onLoaded }
  })

  const reload = useCallback(
    async (arg?: A): Promise<T | null> => {
      setLoading(true)
      setError('')
      try {
        const result = await latest.current.load(arg as A)
        setData(result)
        latest.current.onLoaded?.(result)
        return result
      } catch (err) {
        const text = describeError(err, failMessage)
        setError(text)
        if (!silent) {
          latest.current.fail?.(err, failMessage)
        }
        return null
      } finally {
        setLoading(false)
      }
    },
    [failMessage, silent],
  )

  // 首次挂载自动拉一次
  useEffect(() => {
    void reload()
  }, [reload])

  return { data, loading, error, reload, setData }
}
