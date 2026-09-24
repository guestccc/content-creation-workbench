/**
 * 「评论」弹窗的状态机（本页私有，不进公共 hooks —— 只有素材抓取页用）。
 *
 * 一条笔记一套流程：打开 → 读评论树（顺带拿到原任务的评论配置与**最新一次
 * 补抓任务的状态**）→ 需要时发起补抓 → 轮询到补抓落终态。
 *
 * 四个值得说明的地方：
 *
 * 1. **补抓状态与评论列表同一个接口**：后端把两者挤在一个响应里，所以补抓在跑
 *    期间只轮询这一个接口，列表和进度天然同步 —— 分两个接口会出现「进度说跑完
 *    了、列表还是空的」这种中间态。轮询间隔用公共的 POLL_INTERVAL_MS。
 * 2. **补抓不进历史任务列表**，所以这个弹窗是它唯一的可见入口。用户发起补抓后
 *    把弹窗关掉是很自然的（要等几分钟），因此**关掉弹窗不停轮询**：继续盯着，
 *    跑完弹一条提示，表格里那颗角标也跟着转/停。没有在跑的补抓时才真正卸载。
 * 3. **过期响应要丢**（seqRef）：翻页/换笔记/关弹窗都会让在途的那次请求作废，
 *    回来时先比对序号。与 useAiCopy 同一套口径，只是这边没有流式帧。
 * 4. **失败分两处摆**：读评论失败摆在弹窗里（要能重试），发起补抓失败弹 toast
 *    （用户点的是弹窗底部的按钮，弹窗里再摆一块错误区反而挡住内容）。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError, describeError } from '../../api/client'
import { fetchCrawlCookie } from '../../api/crawlCookie'
import { cancelCrawlJob, fetchNoteComments, refetchNoteComments } from '../../api/crawler'
import { POLL_INTERVAL_MS } from '../../hooks'
import { isTerminalStatus } from '../../types/crawler'
import type { CrawlLoginType, CrawlNote, NoteCommentsData } from '../../types/crawler'

/** 弹窗里的两个视图：看评论 / 设置补抓 */
export type CommentsView = 'list' | 'setup'

/** 补抓时一级评论条数的可选档位（用户在设置视图里单选） */
export const TOP_COMMENT_OPTIONS = [10, 20, 50, 100] as const

/** 正在看哪条笔记的评论 */
interface CommentsTarget {
  /** 结果弹窗那条抓取任务的 id（后端会向上解析到根任务） */
  jobId: number
  note: CrawlNote
}

/**
 * 一级评论条数的默认值：跟着原任务走，但落到可选档位上。
 *
 * 原任务没开评论（max_comments ≤ 0）时给 20 —— 那是默认档，也是多数笔记
 * 一级评论够用的量；比它大的取最接近的一档，别一上来就抓 100 条。
 */
function defaultTopCount(configured: number): number {
  if (configured <= 0) {
    return 20
  }
  return TOP_COMMENT_OPTIONS.find((option) => option >= configured) ?? 100
}

export interface UseNoteCommentsOptions {
  /** 轻提示实例（message.useMessage() 的 message） */
  message: {
    success: (text: string) => void
    info: (text: string) => void
    error: (text: string) => void
  }
  /** 失败处理（useApiMessage().fail），用于发起/取消补抓这类被动作**
   *  之外的操作失败 */
  fail: (error: unknown, fallback: string) => void
}

export interface UseNoteCommentsResult {
  /** 正在看哪条笔记；null = 没在跟踪任何一条 */
  target: CommentsTarget | null
  /** 弹窗是否显示（关掉弹窗但补抓还在跑时 target 仍保留，见文件头注释第 2 条） */
  visible: boolean
  /** 评论数据；null = 还没拿到（首次加载中，或读失败了，见 error） */
  data: NoteCommentsData | null
  loading: boolean
  /** 读评论失败的原因；空串表示没出错 */
  error: string
  view: CommentsView
  /** 发起补抓的请求是否在途（按钮转圈用） */
  submitting: boolean
  /** 补抓设置：一级评论条数 */
  maxComments: number
  /** 补抓设置：是否连二级评论一起抓 */
  subComments: boolean
  /** 补抓设置：登录方式（默认沿用原任务；cookie 过期时换扫码是最常见的解法） */
  loginType: CrawlLoginType
  /** 补抓设置：新贴的 Cookie 串（留空 = 沿用原任务存的） */
  cookies: string
  /** 补抓设置：是否无头跑浏览器 */
  headless: boolean
  /** 有补抓任务在跑（pending / running）—— 弹窗和表格角标都看它 */
  refetchLive: boolean
  /** 正在补抓的那条笔记 id；没有补抓在跑时为 null（结果表给按钮加角标用） */
  activeNoteId: string | null
  setMaxComments: (value: number) => void
  setSubComments: (value: boolean) => void
  setLoginType: (value: CrawlLoginType) => void
  setCookies: (value: string) => void
  setHeadless: (value: boolean) => void
  /** 从 Cookie 库选一条：按 ID 拿完整串回填输入框（列表里只有预览，完整串要单独取） */
  selectCookie: (id: number) => void
  /** 打开某条笔记的评论弹窗 */
  open: (jobId: number, note: CrawlNote) => void
  /** 关弹窗（补抓在跑时只收起来，不停轮询） */
  close: () => void
  /** 切到「补抓设置」视图 */
  showSetup: () => void
  /** 从设置视图退回列表 */
  backToList: () => void
  /** 按当前设置发起补抓 */
  startRefetch: () => void
  /** 取消正在跑的那条补抓任务 */
  cancelRefetch: () => void
  /** 手工重读一次（读失败后的「重试」按钮用） */
  refresh: () => void
}

export function useNoteComments({ message, fail }: UseNoteCommentsOptions): UseNoteCommentsResult {
  const [target, setTarget] = useState<CommentsTarget | null>(null)
  const [visible, setVisible] = useState(false)
  const [data, setData] = useState<NoteCommentsData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [view, setView] = useState<CommentsView>('list')
  const [submitting, setSubmitting] = useState(false)
  const [maxComments, setMaxComments] = useState<number>(20)
  const [subComments, setSubComments] = useState(true)
  const [loginType, setLoginType] = useState<CrawlLoginType>('qrcode')
  const [cookies, setCookies] = useState('')
  const [headless, setHeadless] = useState(false)

  /** 当前这一轮的序号；响应回来对不上就丢弃（见文件头注释第 3 条） */
  const seqRef = useRef(0)
  /** 正在跟踪的这条（回调里要读最新值，不能依赖 state 闭包） */
  const targetRef = useRef<CommentsTarget | null>(null)
  /** 上一次看到的补抓状态：用来识别「刚刚跑完」，好给一次提示 */
  const prevRefetchRef = useRef<NoteCommentsData['refetch']>(null)
  /** 最新一次的 data / visible，供回调读取（避免把 state 写进依赖数组） */
  const latestRef = useRef({ data: null as NoteCommentsData | null, visible: false })
  useEffect(() => {
    latestRef.current = { data, visible }
  })

  /** 把一批新数据写进状态，并在「补抓刚跑完」时给一次回音 */
  const applyData = useCallback(
    (fresh: NoteCommentsData) => {
      const before = prevRefetchRef.current
      const after = fresh.refetch
      prevRefetchRef.current = after
      setData(fresh)

      // 从「在跑」变成终态，且用户已经把弹窗关了（还开着他自己看得见）——
      // 这是补抓唯一的完成回音，没有它用户只能干等或反复点开弹窗
      const finished =
        before !== null &&
        !isTerminalStatus(before.status) &&
        after !== null &&
        isTerminalStatus(after.status)
      if (!finished) {
        return
      }
      const silent = !latestRef.current.visible
      if (after.status === 'success') {
        if (silent) {
          message.success(`评论补抓完成，新增 ${after.comment_count} 条`)
        }
      } else if (after.status === 'failed') {
        message.error(`评论补抓失败：${after.error_message || '可在弹窗里重试'}`)
      } else if (silent) {
        message.info('评论补抓已取消')
      }
    },
    [message],
  )

  /** 丢弃当前跟踪（关弹窗且没有补抓在跑时收尾用） */
  const discard = useCallback(() => {
    seqRef.current += 1
    targetRef.current = null
    prevRefetchRef.current = null
    setTarget(null)
    setData(null)
    setError('')
    setView('list')
  }, [])

  /**
   * 拉一次评论并写回状态。
   *
   * @param seq 发起时的轮次号；回来时对不上说明用户已经换到别的笔记了，整批丢弃
   */
  const fetchInto = useCallback(
    async (jobId: number, noteId: string, seq: number): Promise<NoteCommentsData | null> => {
      try {
        const fresh = await fetchNoteComments(jobId, noteId)
        if (seq !== seqRef.current) {
          return null
        }
        applyData(fresh)
        setError('')
        return fresh
      } catch (caught) {
        if (seq !== seqRef.current) {
          return null
        }
        // 读失败摆在弹窗里而不是 toast：它要配一个「重试」按钮
        setError(describeError(caught, '读取评论失败'))
        return null
      } finally {
        if (seq === seqRef.current) {
          setLoading(false)
        }
      }
    },
    [applyData],
  )

  /** 重读当前这条（轮询与「重试」按钮共用） */
  const reload = useCallback(async () => {
    const current = targetRef.current
    if (current === null) {
      return
    }
    await fetchInto(current.jobId, current.note.id, seqRef.current)
  }, [fetchInto])

  const open = useCallback(
    (jobId: number, note: CrawlNote) => {
      const seq = seqRef.current + 1
      seqRef.current = seq
      const next = { jobId, note }
      targetRef.current = next
      prevRefetchRef.current = null
      setTarget(next)
      setVisible(true)
      setView('list')
      setData(null)
      setError('')
      setLoading(true)

      void (async () => {
        const fresh = await fetchInto(jobId, note.id, seq)
        if (fresh === null || seq !== seqRef.current) {
          return
        }
        // 条数默认跟原任务走（落到可选档位上）；二级默认开 —— 带货选品真正
        // 有价值的信息大多在二级评论里，默认关等于让人每次都手动打开
        setMaxComments(defaultTopCount(fresh.comments_config.max_comments))
        setSubComments(true)
        // 登录方式 / 无头默认沿用原任务；cookie 输入框永远从空开始 ——
        // 凭据不回显是安全约定，「留空 = 沿用存的串」由后端兜住
        setLoginType(fresh.login_type)
        setHeadless(fresh.headless)
        setCookies('')
      })()
    },
    [fetchInto],
  )

  const close = useCallback(() => {
    setVisible(false)
    const current = latestRef.current.data
    const live = current?.refetch != null && !isTerminalStatus(current.refetch.status)
    // 补抓还在跑：留着 target 继续轮询（表格角标靠它转），跑完自己收尾。
    // 没有在跑的就立刻卸载，别让一条已经看完的笔记占着内存
    if (!live) {
      discard()
    }
  }, [discard])

  const showSetup = useCallback(() => setView('setup'), [])
  const backToList = useCallback(() => setView('list'), [])

  const startRefetch = useCallback(async () => {
    const current = targetRef.current
    if (current === null) {
      return
    }
    setSubmitting(true)
    try {
      const freshCookies = cookies.trim()
      await refetchNoteComments(current.jobId, {
        note_id: current.note.id,
        max_comments: maxComments,
        sub_comments: subComments,
        login_type: loginType,
        headless,
        // 留空不传 = 后端沿用原任务存的串（凭据不回显，前端没法预填）
        ...(loginType === 'cookie' && freshCookies !== '' ? { cookies: freshCookies } : {}),
      })
      message.success('已开始补抓评论')
    } catch (caught) {
      // 409：这条笔记已经有一个在跑的补抓（双击、或者另一个标签页刚点过）。
      // 这不是失败 —— 刷新一下就能看到那条的状态，等价于幂等
      if (caught instanceof ApiError && caught.status === 409) {
        message.info('这条笔记已经有补抓在进行')
      } else {
        fail(caught, '发起补抓失败')
      }
    } finally {
      setSubmitting(false)
    }
    // 无论成败都退回列表视图并刷一次：成功要立刻显示「正在补抓」，
    // 409 也要把别人发起的那条显示出来
    setView('list')
    await reload()
  }, [maxComments, subComments, loginType, headless, cookies, message, fail, reload])

  const cancelRefetch = useCallback(async () => {
    const jobId = latestRef.current.data?.refetch?.job_id
    if (jobId === undefined) {
      return
    }
    try {
      await cancelCrawlJob(jobId)
      message.info('已请求取消，正在结束抓取进程')
    } catch (caught) {
      fail(caught, '取消补抓失败')
    }
    await reload()
  }, [message, fail, reload])

  // 不复用 cookieLib.select：那边会把 selectedId 记到建任务表单的选择框上，
  // 从补抓弹窗里选不该改动表单的选中态（两边的选择框只是各自的插入器）
  const selectCookie = useCallback(
    async (id: number) => {
      try {
        const detail = await fetchCrawlCookie(id)
        setCookies(detail.cookie)
      } catch (caught) {
        fail(caught, '读取 Cookie 失败')
      }
    },
    [fail],
  )

  const refetch = data?.refetch ?? null
  const refetchLive = refetch !== null && !isTerminalStatus(refetch.status)
  /** 换一条笔记 / 补抓开始或结束时才重建定时器（进度更新不重建，与 useJobPolling 同口径） */
  const noteKey = target === null ? null : `${target.jobId}:${target.note.id}`

  useEffect(() => {
    if (noteKey === null || !refetchLive) {
      return
    }
    const timer = window.setInterval(() => {
      void reload()
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [noteKey, refetchLive, reload])

  return {
    target,
    visible,
    data,
    loading,
    error,
    view,
    submitting,
    maxComments,
    subComments,
    loginType,
    cookies,
    headless,
    refetchLive,
    activeNoteId: refetchLive ? (target?.note.id ?? null) : null,
    setMaxComments,
    setSubComments,
    setLoginType,
    setCookies,
    setHeadless,
    selectCookie: (id: number) => void selectCookie(id),
    open,
    close,
    showSetup,
    backToList,
    startRefetch: () => void startRefetch(),
    cancelRefetch: () => void cancelRefetch(),
    refresh: () => void reload(),
  }
}
