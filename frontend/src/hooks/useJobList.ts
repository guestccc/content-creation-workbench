/**
 * 分页的任务列表（三个任务型页面的「历史任务」表格）。
 *
 * 列表加载失败一律静默：历史只是回看用的，拉不到不该打断页面上正在做的事，
 * 表格空着就是了。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

/** 后端分页列表的统一形状（镜头分割 / 字幕提取 / 混剪都一样） */
export interface PagedList<J> {
  total: number
  page: number
  page_size: number
  items: J[]
}

export interface UseJobListOptions<J> {
  /** 拉取某页（各页面的 fetchXxxJobs） */
  fetchList: (params: { page: number; page_size: number }) => Promise<PagedList<J>>
  pageSize?: number
}

export interface UseJobListResult<J> {
  items: J[]
  total: number
  loading: boolean
  page: number
  setPage: (page: number) => void
  /** 重新拉当前页（任务增删改后调用） */
  reload: () => void
}

export function useJobList<J>({ fetchList, pageSize = 10 }: UseJobListOptions<J>): UseJobListResult<J> {
  const [data, setData] = useState<PagedList<J> | null>(null)
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  /** 递增触发重新拉取：reload 的引用因此可以保持稳定 */
  const [tick, setTick] = useState(0)

  const latest = useRef({ fetchList, pageSize })
  useEffect(() => {
    latest.current = { fetchList, pageSize }
  })

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    void (async () => {
      try {
        const result = await latest.current.fetchList({
          page,
          page_size: latest.current.pageSize,
        })
        if (!cancelled) {
          setData(result)
        }
      } catch {
        // 静默：见文件头
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [page, tick])

  const reload = useCallback(() => setTick((value) => value + 1), [])

  return {
    items: data?.items ?? [],
    total: data?.total ?? 0,
    loading,
    page,
    setPage,
    reload,
  }
}
