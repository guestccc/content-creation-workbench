/**
 * 一条抓取任务派生出来的换背景任务，按**笔记 id** 分好组（本页私有）。
 *
 * 用途只有一个：结果弹窗里某条笔记的图要是被拿去换过背景，那一行的操作列就能多出
 * 一个「换背景任务 N」的按钮。所以这里不要明细、不要统计，只要「哪条笔记有任务」。
 *
 * 取数走**列表接口**按 `source_crawl_job_id` 过滤，一次把这条抓取任务派生的全部
 * 换背景任务捞回来 —— 不能反过来「先列全部再把不是本任务的挑出来」（分页会把
 * 需要的挤到后面的页里），也不能每条笔记问一次（几十条笔记就是几十个请求）。
 */

import { useEffect, useRef, useState } from 'react'

import { describeError } from '../../api/client'
import { fetchBackgroundJobs } from '../../api/background'
import type { BackgroundJob } from '../../types/background'

/** 单页条数（后端上限 100） */
const PAGE_SIZE = 100
/**
 * 最多翻几页。
 *
 * 一条抓取任务派生出的换背景任务正常就是几条到几十条，一页足够；封顶只影响
 * 「某条笔记的按钮上少算了几条」，不会让查询出错，所以到顶就算了，不做无界翻页。
 */
const MAX_PAGES = 3

export interface UseDerivedBackgroundJobsResult {
  /** 笔记 id → 该笔记派生出的换背景任务（新的在前） */
  byNoteId: Record<string, BackgroundJob[]>
  loading: boolean
  /**
   * 查询失败的原因；成功（含「确实一条都没有」）时是 null。
   *
   * 页面拿它区分「没查到」与「查到 0 条」—— 两者在 byNoteId 上长得一样
   * （都是空对象），但一个是后端出问题、一个是本来就没人换过背景，弹窗里
   * 给的提示完全不同。
   */
  error: string | null
  refresh: () => void
}

/**
 * @param crawlJobId 结果弹窗开着的那条抓取任务；null 表示没开弹窗
 * @param fail 失败处理（useApiMessage().fail）
 */
export function useDerivedBackgroundJobs({
  crawlJobId,
  fail,
}: {
  crawlJobId: number | null
  fail: (error: unknown, fallback: string) => void
}): UseDerivedBackgroundJobsResult {
  const [byNoteId, setByNoteId] = useState<Record<string, BackgroundJob[]>>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** 手动刷新的计数器（弹窗里的「刷新」按钮） */
  const [tick, setTick] = useState(0)

  /** 代次：连着切两条任务时，先发起的那次回来得晚不能盖掉新的 */
  const token = useRef(0)

  useEffect(() => {
    if (crawlJobId === null) {
      // 弹窗关了就把分组清掉：下次打开的是另一条任务，留着旧分组只会串数据
      setByNoteId({})
      setError(null)
      return
    }

    const current = (token.current += 1)
    setLoading(true)

    void (async () => {
      const items: BackgroundJob[] = []
      try {
        for (let page = 1; page <= MAX_PAGES; page += 1) {
          const data = await fetchBackgroundJobs({
            source_crawl_job_id: crawlJobId,
            page,
            page_size: PAGE_SIZE,
          })
          items.push(...data.items)
          // 拉够了 / 后端说没有了 / 这一页是空的，三种都停
          if (items.length >= data.total || data.items.length === 0) {
            break
          }
        }
      } catch (caught) {
        if (current !== token.current) {
          return
        }
        setByNoteId({})
        setError(describeError(caught, '读取换背景任务失败'))
        fail(caught, '读取换背景任务失败')
        setLoading(false)
        return
      }

      if (current !== token.current) {
        return
      }

      const grouped: Record<string, BackgroundJob[]> = {}
      for (const job of items) {
        // 空串 = 这条换背景任务不是从抓取结果建的，挂不到任何一行。
        // 老版本后端根本不认 source_crawl_job_id 这个参数（会把它当没传、返回
        // **全部**任务），那时响应里也没有来源字段 —— 走的就是这个分支：一条都
        // 挂不上去、按钮全不出现，而不是把不相干的任务挂到笔记上。
        if (!job.source_crawl_note_id) {
          continue
        }
        const bucket = grouped[job.source_crawl_note_id]
        if (bucket) {
          bucket.push(job)
        } else {
          grouped[job.source_crawl_note_id] = [job]
        }
      }
      setByNoteId(grouped)
      setError(null)
      setLoading(false)
    })()
  }, [crawlJobId, tick, fail])

  return { byNoteId, loading, error, refresh: () => setTick((value) => value + 1) }
}
