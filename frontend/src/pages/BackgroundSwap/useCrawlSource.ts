/**
 * 「从素材抓取选图」弹窗的两段式取数（本页私有）。
 *
 * 选到一条笔记要经过两次选择，所以这里是个小状态机：
 *   1. 列**可用的抓取任务** —— 终态（跑起来的产物不全）且 note_count > 0
 *      （没抓到内容的任务点进去也是空的）；
 *   2. 选中任务后拉它的结果，只留**有本地图片**的笔记 —— 换背景要的就是图，
 *      没图的笔记进来只会让用户白点一下。
 *
 * 只 import 抓取域的 api 与类型，不 import 抓取页本身（页面文件夹之间零互引）。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { fetchCrawlJobs, fetchCrawlResults } from '../../api/crawler'
import type { BackgroundSwapPrefill } from '../../types/background'
import { isTerminalStatus as isCrawlTerminalStatus } from '../../types/crawler'
import type { CrawlJob, CrawlNote } from '../../types/crawler'
import { prefillFromCrawlNote } from '../../utils/crawlPrefill'

/**
 * 任务下拉一次拉这么多条。
 *
 * 抓取任务攒到几百条是常态，但**越新的越可能用**，而这里只要「选一条最近跑的」。
 * 拉回来的还要按「终态 + 有内容」过滤，100 条够覆盖到眼前要用的那几条。
 */
const JOBS_PAGE_SIZE = 100

export interface UseCrawlSourceResult {
  open: boolean
  /** 可供选择的抓取任务（终态且有内容），新的在前 */
  jobs: CrawlJob[]
  jobsLoading: boolean
  /** 当前选中的抓取任务 id；null 表示一条可选的都没有 */
  activeJobId: number | null
  /** 当前任务里有本地图片的笔记 */
  notes: CrawlNote[]
  notesLoading: boolean
  openModal: () => void
  closeModal: () => void
  selectJob: (jobId: number) => void
  /** 选中一条笔记：翻成带入载荷交给页面（`onPick`），然后关掉弹窗 */
  pick: (note: CrawlNote) => void
}

/**
 * @param onPick 选好一条笔记后拿到带入载荷（页面拿去填目录与勾选）
 * @param fail 失败处理（useApiMessage().fail）
 */
export function useCrawlSource({
  onPick,
  fail,
}: {
  onPick: (prefill: BackgroundSwapPrefill) => void
  fail: (error: unknown, fallback: string) => void
}): UseCrawlSourceResult {
  const [open, setOpen] = useState(false)
  const [jobs, setJobs] = useState<CrawlJob[]>([])
  const [jobsLoading, setJobsLoading] = useState(false)
  const [activeJobId, setActiveJobId] = useState<number | null>(null)
  const [notes, setNotes] = useState<CrawlNote[]>([])
  const [notesLoading, setNotesLoading] = useState(false)

  /**
   * 当前选中的任务 id 的最新值。
   *
   * openModal 里要看它（重开弹窗时尽量停在原来那条任务上），但那是个没有依赖
   * 数组的回调 —— 直接读 state 会读到闭包里的旧值，所以走 ref。
   */
  const activeJobIdRef = useRef<number | null>(null)
  useEffect(() => {
    activeJobIdRef.current = activeJobId
  }, [activeJobId])

  /**
   * 笔记列表的代次：连着换任务时，先发起的那次回来得晚就会盖掉新的 ——
   * 用户看到的会是「选着 A 任务、列的是 B 任务的笔记」。
   */
  const notesToken = useRef(0)

  /**
   * onPick 进 ref（「最新 ref」模式）：页面传进来的是个每次渲染都重建的普通函数，
   * 直接写进依赖数组的话 pick 等于没包 useCallback，引用每帧都在变。
   */
  const onPickRef = useRef(onPick)
  useEffect(() => {
    onPickRef.current = onPick
  }, [onPick])

  const loadNotes = useCallback(
    async (jobId: number) => {
      const token = (notesToken.current += 1)
      setNotesLoading(true)
      try {
        const data = await fetchCrawlResults(jobId)
        if (token !== notesToken.current) {
          return
        }
        setNotes(
          data.notes.filter(
            (note) => note.local_image_dir !== '' && note.local_images.length > 0,
          ),
        )
      } catch (error) {
        if (token !== notesToken.current) {
          return
        }
        setNotes([])
        fail(error, '读取抓取结果失败')
      } finally {
        if (token === notesToken.current) {
          setNotesLoading(false)
        }
      }
    },
    [fail],
  )

  /** 拉一次可选任务列表；返回过滤后的结果给调用方挑默认选中项 */
  const loadJobs = useCallback(async (): Promise<CrawlJob[]> => {
    setJobsLoading(true)
    try {
      const data = await fetchCrawlJobs({ page: 1, page_size: JOBS_PAGE_SIZE })
      const usable = data.items.filter(
        (job) => isCrawlTerminalStatus(job.status) && job.note_count > 0,
      )
      setJobs(usable)
      return usable
    } catch (error) {
      setJobs([])
      fail(error, '读取素材抓取任务失败')
      return []
    } finally {
      setJobsLoading(false)
    }
  }, [fail])

  /**
   * 打开弹窗并重新取数：这中间用户可能刚跑完一条抓取任务，列表不该是进页面
   * 那一刻的快照。原来选中那条还在的话就停在它上面，否则落到最新的一条。
   */
  const openModal = useCallback(() => {
    setOpen(true)
    void (async () => {
      const usable = await loadJobs()
      const previous = activeJobIdRef.current
      const target = usable.find((job) => job.id === previous) ?? usable[0]
      if (!target) {
        setActiveJobId(null)
        setNotes([])
        return
      }
      setActiveJobId(target.id)
      void loadNotes(target.id)
    })()
  }, [loadJobs, loadNotes])

  const selectJob = useCallback(
    (jobId: number) => {
      setActiveJobId(jobId)
      void loadNotes(jobId)
    },
    [loadNotes],
  )

  const closeModal = useCallback(() => {
    // 下一次打开会重新拉：这里的下拉是「选一条任务」，留着旧数据只会误导
    setOpen(false)
  }, [])

  const pick = useCallback((note: CrawlNote) => {
    const jobId = activeJobIdRef.current
    // 列表按「有本地图片」筛过，翻不出 null；真翻不出来就当没点（不给用户
    // 报错 —— 报的也是句他没法处理的废话）
    const prefill = jobId === null ? null : prefillFromCrawlNote(note, jobId)
    if (prefill === null) {
      return
    }
    onPickRef.current(prefill)
    setOpen(false)
  }, [])

  return {
    open,
    jobs,
    jobsLoading,
    activeJobId,
    notes,
    notesLoading,
    openModal,
    closeModal,
    selectJob,
    pick,
  }
}
