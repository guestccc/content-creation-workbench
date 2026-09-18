/**
 * 结果弹窗与日志弹窗的数据（本页私有，不进公共 hooks）。
 *
 * 两个独立的小状态机：结果表（open 时拉 results 接口）与日志尾部
 * （点开时拉 log 接口）。页面只负责把 open / close / loadLog 接到按钮上。
 */

import { useCallback, useState } from 'react'

import { fetchCrawlLog, fetchCrawlResults } from '../../api/crawler'
import type { CrawlNote } from '../../types/crawler'

export function useCrawlResults(fail: (error: unknown, fallback: string) => void) {
  // ---------- 结果弹窗 ----------
  const [jobId, setJobId] = useState<number | null>(null)
  const [notes, setNotes] = useState<CrawlNote[]>([])
  const [loading, setLoading] = useState(false)

  // ---------- 日志弹窗 ----------
  const [logJobId, setLogJobId] = useState<number | null>(null)
  const [logText, setLogText] = useState('')
  const [logLoading, setLogLoading] = useState(false)

  /** 打开某条任务的结果（拉完才开，失败不弹空窗） */
  const open = useCallback(
    async (id: number) => {
      setLoading(true)
      try {
        const data = await fetchCrawlResults(id)
        setNotes(data.notes)
        setJobId(id)
      } catch (error) {
        fail(error, '读取抓取结果失败')
      } finally {
        setLoading(false)
      }
    },
    [fail],
  )

  const close = useCallback(() => {
    setJobId(null)
    setNotes([])
  }, [])

  /** 删除历史记录后，如果结果弹窗正开着这条任务，一并关掉避免残留 */
  const closeIfJob = useCallback((id: number) => {
    if (jobId === id) {
      setJobId(null)
      setNotes([])
    }
  }, [jobId])

  /** 拉某条任务的日志尾部并弹窗（排查失败用） */
  const loadLog = useCallback(
    async (id: number) => {
      setLogLoading(true)
      try {
        const data = await fetchCrawlLog(id)
        setLogText(data.log)
        setLogJobId(id)
      } catch (error) {
        fail(error, '读取日志失败')
      } finally {
        setLogLoading(false)
      }
    },
    [fail],
  )

  const closeLog = useCallback(() => {
    setLogJobId(null)
    setLogText('')
  }, [])

  return {
    jobId,
    notes,
    loading,
    open,
    close,
    closeIfJob,
    logOpen: logJobId !== null,
    logLoading,
    logText,
    loadLog,
    closeLog,
  }
}
