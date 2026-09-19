/**
 * 分页的任务列表（任务型页面的「历史任务」表格）：分页 + 行多选。
 *
 * 列表加载失败一律静默：历史只是回看用的，拉不到不该打断页面上正在做的事，
 * 表格空着就是了。
 *
 * 多选的三条规则（批量删除用）：
 * - 翻页必清空 —— 选择永远只在当前页内，不做跨页累积；
 * - 刷新只修剪不清空 —— 轮询期间 running→finished 的行保持选中，
 *   但已从本页消失的行（被别处删掉）被剪掉；
 * - 批量删除成功后由页面显式 clearSelection，不等下一次刷新。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

/** 后端分页列表的统一形状（镜头分割 / 字幕提取 / 混剪都一样） */
export interface PagedList<J> {
  total: number
  page: number
  page_size: number
  items: J[]
}

export interface UseJobListOptions<J extends { id: number }> {
  /** 拉取某页（各页面的 fetchXxxJobs） */
  fetchList: (params: { page: number; page_size: number }) => Promise<PagedList<J>>
  pageSize?: number
}

export interface UseJobListResult<J extends { id: number }> {
  items: J[]
  total: number
  loading: boolean
  page: number
  /** 翻页（同时清空行多选：选择不跨页累积） */
  setPage: (page: number) => void
  /** 重新拉当前页（任务增删改后调用） */
  reload: () => void
  /** 行多选状态（antd Table rowSelection 用） */
  selectedRowKeys: number[]
  setSelectedRowKeys: (keys: number[]) => void
  /** 清空多选（批量删除成功后调用） */
  clearSelection: () => void
}

export function useJobList<J extends { id: number }>({
  fetchList,
  pageSize = 10,
}: UseJobListOptions<J>): UseJobListResult<J> {
  const [data, setData] = useState<PagedList<J> | null>(null)
  const [loading, setLoading] = useState(false)
  const [page, setPageRaw] = useState(1)
  /** 递增触发重新拉取：reload 的引用因此可以保持稳定 */
  const [tick, setTick] = useState(0)
  const [selectedRowKeys, setSelectedRowKeys] = useState<number[]>([])

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
          // 只修剪已消失的行，不清空：running→finished 的行不该丢选择
          const alive = new Set(result.items.map((item) => item.id))
          setSelectedRowKeys((prev) => {
            const next = prev.filter((key) => alive.has(key))
            return next.length === prev.length ? prev : next
          })
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
  const setPage = useCallback((value: number) => {
    setSelectedRowKeys([])
    setPageRaw(value)
  }, [])
  const clearSelection = useCallback(() => setSelectedRowKeys([]), [])

  return {
    items: data?.items ?? [],
    total: data?.total ?? 0,
    loading,
    page,
    setPage,
    reload,
    selectedRowKeys,
    setSelectedRowKeys,
    clearSelection,
  }
}
