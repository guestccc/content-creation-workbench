/**
 * 一键成品页面的主流程状态（单页私有，不进公共 hooks）。
 *
 * 两步状态机：① 选素材（字幕 + 成片）→ ② AI 文案（生成 / 改字 / 复制）。
 * 文案任务的生命周期复用公共的 useJobRunner；候选文案用户改过的字在这里 ——
 * 页面只编排。
 *
 * **烧录（原第 ③ 步）已从页面摘掉**：文案生成完就结束，用户拿着复制出来的
 * 稿子回剪映人工烧字 + 配音。后端 render 接口/表/服务都还在（只摘页面），
 * 所以这里删掉的渲染半边不会再被页面用到，也不影响接口。
 */

import { useCallback, useMemo, useRef, useState } from 'react'
import { useEffect } from 'react'

import {
  batchDeleteCopyJobs,
  cancelCopyJob,
  createCopyJob,
  deleteCopyJob,
  fetchCopyJob,
  fetchCopyJobSubtitleText,
  fetchFinalcutSources,
  retryCopyJob,
} from '../../api/finalcut'
import { describeError } from '../../api/client'
import { useJobRunner } from '../../hooks/useJobRunner'
import type {
  CopyJobPayload,
  FinalcutCopyJob,
  FinalcutCopySubtitleText,
  FinalcutSources,
} from '../../types/finalcut'
import { isTerminalStatus } from '../../types/finalcut'
import type { UseApiMessageResult } from '../../hooks/useApiMessage'

/** 一份已选定的素材（字幕或成片） */
export interface SelectedMaterial {
  /** 绝对路径（创建任务时后端还会再校验一遍） */
  path: string
  /** 展示名（历史产物带「混剪 #1 / 01.mp4」这类来源说明） */
  name: string
  /** 来源：历史任务产物 / 本地任意文件 */
  origin: 'history' | 'local'
  duration_seconds: number | null
  size_bytes: number
}

/** 一条候选文案在页面上的状态（AI 产物 + 用户改过的字） */
export interface CandidateState {
  /** 对应 copyJob.result.copies 的下标 */
  key: number
  /** 文案正文（用户可改字；初始为 AI 给的原文） */
  text: string
}

export interface UseFinalcutFlowResult {
  /** 当前在第几步（0 选素材 / 1 AI 文案） */
  step: number
  subtitle: SelectedMaterial | null
  video: SelectedMaterial | null
  selectSubtitle: (item: SelectedMaterial) => void
  selectVideo: (item: SelectedMaterial) => void
  clearSubtitle: () => void
  clearVideo: () => void
  /** 第 ① 步的校验：空串表示可以生成文案 */
  validationError: string
  /**
   * 语速输入框的值（三态）：null = 跟随全局默认（提交时不带字段，后端按
   * 当前全局值快照）；数字 = 这条任务念快/念慢的覆盖值；清空输入框回到 null。
   * 它只是「下一次任务」的值 —— 已建任务生效的是 copyJob.chars_per_second。
   */
  rateOverride: number | null
  setRateOverride: (value: number | null) => void
  /** 历史产物清单（选素材弹窗用） */
  sources: FinalcutSources | null
  sourcesLoading: boolean
  loadSources: () => Promise<void>

  /** 开始生成文案（建 copy job 并进入第 ② 步）；已在跑时会先被拒 */
  startCopyJob: () => Promise<void>
  /** 回到上一步（状态保留，素材不清空） */
  goBack: () => void
  copyJob: FinalcutCopyJob | null
  copyJobRunning: boolean
  copyJobSubmitting: boolean
  /**
   * 当前文案任务用的字幕内容（第 ② 步左列）。
   *
   * 与任务状态无关：排队中/失败/取消时也有 —— 失败时恰恰最需要看
   * 「AI 读到的到底是什么」。任务被删或还没建时为 null。
   */
  subtitleText: FinalcutCopySubtitleText | null
  subtitleTextLoading: boolean
  /** 取字幕失败的原因（空串 = 正常）；由卡片内 Alert 呈现，不弹提示 */
  subtitleTextError: string
  cancelCurrentCopyJob: () => Promise<void>
  /** 「换一批」：同素材再建一个文案任务 */
  regenerate: () => Promise<void>

  /** 候选文案（AI 产物 + 用户改过的字） */
  candidates: CandidateState[]
  updateCandidateText: (key: number, text: string) => void
  /** 从历史列表点开一条文案任务：回填候选并进入第 ② 步 */
  openCopyJob: (jobId: number) => Promise<void>
  /** 删除文案任务记录（磁盘无产物，只删记录） */
  removeCopyJob: (jobId: number) => Promise<boolean>
  /** 批量删除文案任务记录 */
  removeCopyJobs: (ids: number[]) => Promise<boolean>
  /** 取消任意一条文案任务（历史列表用），返回更新后的任务 */
  cancelCopyJobById: (jobId: number) => Promise<FinalcutCopyJob | null>
  /** 重试任意一条文案任务（历史列表用）：按原参数另起一条新任务 */
  retryCopyJobById: (jobId: number) => Promise<void>
}

export interface UseFinalcutFlowOptions {
  /** 文案任务有增删改时回调（页面拿它刷新历史列表） */
  onCopyJobChanged?: () => void
}

export function useFinalcutFlow(
  api: UseApiMessageResult,
  options?: UseFinalcutFlowOptions,
): UseFinalcutFlowResult {
  // ------------------------------------------------------------------
  // 第 ① 步：素材选择
  // ------------------------------------------------------------------
  const [step, setStep] = useState(0)
  const [subtitle, setSubtitle] = useState<SelectedMaterial | null>(null)
  const [video, setVideo] = useState<SelectedMaterial | null>(null)

  const [sources, setSources] = useState<FinalcutSources | null>(null)
  const [sourcesLoading, setSourcesLoading] = useState(false)

  // 语速覆盖值（字/秒）：null = 跟随全局默认。范围与后端 normalize 同一套
  // （1.0–15.0），提交前在本地拦 —— 后端 422 经统一异常处理只剩「请求参数
  // 校验失败」，具体越界原因到不了提示条。
  const [rateOverride, setRateOverride] = useState<number | null>(null)

  const selectSubtitle = useCallback((item: SelectedMaterial) => setSubtitle(item), [])
  const selectVideo = useCallback((item: SelectedMaterial) => setVideo(item), [])
  const clearSubtitle = useCallback(() => setSubtitle(null), [])
  const clearVideo = useCallback(() => setVideo(null), [])

  const validationError = useMemo(() => {
    if (!subtitle) {
      return '请先选择字幕素材'
    }
    if (!video) {
      return '请先选择成片视频'
    }
    return ''
  }, [subtitle, video])

  const loadSources = useCallback(async () => {
    setSourcesLoading(true)
    try {
      setSources(await fetchFinalcutSources())
    } catch (err) {
      api.fail(err, '读取历史产物失败')
      setSources(null)
    } finally {
      setSourcesLoading(false)
    }
  }, [api])

  // ------------------------------------------------------------------
  // 第 ② 步：AI 文案任务生命周期
  // ------------------------------------------------------------------
  const [candidates, setCandidates] = useState<CandidateState[]>([])

  // api 进 ref，保证下面的回调引用恒定（useJobRunner 也走同一套「最新 ref」）
  const apiRef = useRef(api)
  useEffect(() => {
    apiRef.current = api
  })

  // options 同样进 ref：下面的回调要能在引用恒定的前提下拿到最新的刷新函数
  const optionsRef = useRef(options)
  useEffect(() => {
    optionsRef.current = options
  })

  /** 按任务结果重建候选列表（跑完回填 / 从历史点开 共用） */
  const rebuildCandidates = useCallback((job: FinalcutCopyJob) => {
    const copies = job.status === 'success' ? (job.result?.copies ?? []) : []
    setCandidates(copies.map((copy, index) => ({ key: index, text: copy.text })))
    return copies.length
  }, [])

  const copyRunner = useJobRunner<FinalcutCopyJob, CopyJobPayload>({
    create: createCopyJob,
    cancel: cancelCopyJob,
    remove: (jobId) => deleteCopyJob(jobId),
    batchRemove: (ids) => batchDeleteCopyJobs(ids),
    fetchJob: fetchCopyJob,
    isTerminal: (job) => isTerminalStatus(job.status),
    fail: api.fail,
    onChanged: options?.onCopyJobChanged,
    onRemoved: (_jobId, wasCurrent) => {
      if (wasCurrent) {
        setCandidates([])
      }
    },
    onFinished: (job) => {
      // 跑完按结果重建候选列表；失败/取消时 result 为空，候选清空
      const count = rebuildCandidates(job)
      if (job.status === 'success' && count > 0) {
        apiRef.current.message.success(`文案已生成：${count} 条候选`)
      }
    },
  })

  const openCopyJob = useCallback(
    async (jobId: number) => {
      const job = await copyRunner.read(jobId)
      if (!job) {
        return
      }
      copyRunner.setJob(job)
      rebuildCandidates(job)
      // 语速框回填这条任务的值（老任务是 0 = 不可考，回落「跟随默认」）：
      // 在这里「换一批」延续上次的手调值，比每次都重置成全局默认顺手。
      setRateOverride(job.chars_per_second > 0 ? job.chars_per_second : null)
      setStep(1)
    },
    [copyRunner, rebuildCandidates],
  )

  const removeCopyJob = useCallback(
    (jobId: number) => copyRunner.remove(jobId),
    [copyRunner],
  )
  const removeCopyJobs = useCallback(
    (ids: number[]) => copyRunner.removeMany(ids),
    [copyRunner],
  )

  const startCopyJob = useCallback(async () => {
    if (!subtitle || !video || copyRunner.running) {
      return
    }
    // InputNumber 的 min/max 拦不住手输后失焦的越界值（typed 场景），提交前
    // 自己再拦一遍 —— 区间与后端 normalize_chars_per_second 一致。
    if (rateOverride != null && (rateOverride < 1 || rateOverride > 15)) {
      api.message.error('口播语速请在 1.0–15.0 字/秒之间')
      return
    }
    const created = await copyRunner.submit({
      subtitle_path: subtitle.path,
      video_path: video.path,
      ...(rateOverride != null ? { chars_per_second: rateOverride } : {}),
    })
    if (created) {
      setCandidates([])
      setStep(1)
    }
  }, [subtitle, video, copyRunner, rateOverride, api])

  const regenerate = useCallback(async () => {
    // 换一批 = 同素材再跑一个任务；上一个任务保留在历史里，不删
    await startCopyJob()
  }, [startCopyJob])

  const cancelCurrentCopyJob = useCallback(async () => {
    if (copyRunner.job) {
      await copyRunner.cancel(copyRunner.job.id)
    }
  }, [copyRunner])

  /** 取消任意一条文案任务（历史列表里的取消按钮用；onChanged 会刷新历史） */
  const cancelCopyJobById = useCallback(
    (jobId: number) => copyRunner.cancel(jobId),
    [copyRunner],
  )

  /**
   * 重试任意一条文案任务（历史列表里的重试按钮用）：后端按原参数**新建一条
   * 任务**并返回它（新 id、新一批文案）。
   *
   * 文案任务没有条目级状态（一次 AI 调用产出一批文案），只能整任务重跑；
   * 而必须是新建 —— 一条任务的产物就是「那一批文案」，就地重跑会把上一批
   * 覆盖掉，历史记录与产物再也对不上。
   *
   * 刻意**不把新任务顶成当前任务**：用户此刻在翻历史，不该被拽到第 ② 步去。
   * 刷新交给 onCopyJobChanged（页面传的是历史列表的 reload）。
   */
  const retryCopyJobById = useCallback(async (jobId: number) => {
    try {
      const created = await retryCopyJob(jobId)
      optionsRef.current?.onCopyJobChanged?.()
      apiRef.current.message.success(`已重新发起：新任务 #${created.id}`)
    } catch (error) {
      apiRef.current.fail(error, '重试失败')
    }
  }, [])

  // ------------------------------------------------------------------
  // 第 ② 步左列：当前任务用的字幕（原文 + 喂给 AI 的素材）
  // ------------------------------------------------------------------
  const [subtitleText, setSubtitleText] = useState<FinalcutCopySubtitleText | null>(null)
  const [subtitleTextLoading, setSubtitleTextLoading] = useState(false)
  const [subtitleTextError, setSubtitleTextError] = useState('')

  // 依赖只写任务 id（CLAUDE.md 第 5 条）：以整个任务对象为依赖的话，轮询每刷
  // 一次进度都会重下一遍字幕（可能有几百 KB）。
  const copyJobId = copyRunner.job?.id ?? null
  useEffect(() => {
    if (copyJobId === null) {
      setSubtitleText(null)
      setSubtitleTextError('')
      return
    }

    // 换任务时旧请求仍可能返回，用标志位丢掉它 —— 否则慢的那条会盖掉新的
    let cancelled = false
    setSubtitleTextLoading(true)
    setSubtitleTextError('')
    fetchCopyJobSubtitleText(copyJobId)
      .then((data) => {
        if (!cancelled) {
          setSubtitleText(data)
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setSubtitleText(null)
          // 不弹提示：这是「任务一变就自动拉」的隐式加载，不是用户主动点的，
          // 而历史任务的字幕文件常已被搬走 —— 每点开一次弹个红条是噪音。
          // 文案本身照常可看，只在卡片里说明字幕读不到。
          setSubtitleTextError(describeError(error, '读取字幕素材失败'))
        }
      })
      .finally(() => {
        if (!cancelled) {
          setSubtitleTextLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [copyJobId])

  const goBack = useCallback(() => {
    // 只退步数：语速覆盖值**刻意不清** —— 输入框就在按钮上方、值始终可见，
    // 换素材时被悄悄重置回全局默认，比「沿用上次的手调值」更恼人。
    setStep((current) => Math.max(0, current - 1))
  }, [])

  // ------------------------------------------------------------------
  // 候选文案的编辑（只有正文可改：复制出去的是这段文字本身）
  // ------------------------------------------------------------------
  const updateCandidateText = useCallback((key: number, text: string) => {
    setCandidates((list) =>
      list.map((item) => (item.key === key ? { ...item, text } : item)),
    )
  }, [])

  return {
    step,
    subtitle,
    video,
    selectSubtitle,
    selectVideo,
    clearSubtitle,
    clearVideo,
    validationError,
    rateOverride,
    setRateOverride,
    sources,
    sourcesLoading,
    loadSources,
    startCopyJob,
    goBack,
    copyJob: copyRunner.job,
    copyJobRunning: copyRunner.running,
    copyJobSubmitting: copyRunner.submitting,
    subtitleText,
    subtitleTextLoading,
    subtitleTextError,
    cancelCurrentCopyJob,
    regenerate,
    candidates,
    updateCandidateText,
    openCopyJob,
    removeCopyJob,
    removeCopyJobs,
    cancelCopyJobById,
    retryCopyJobById,
  }
}

/** 把一条历史产物来源转成 SelectedMaterial（origin 字段统一折成 history） */
export function sourceToMaterial(source: {
  path: string
  name: string
  duration_seconds: number | null
  size_bytes: number
}): SelectedMaterial {
  return {
    path: source.path,
    name: source.name,
    origin: 'history',
    duration_seconds: source.duration_seconds,
    size_bytes: source.size_bytes,
  }
}

/** 本地文件只有路径可知：名字取文件名，时长/大小等创建任务时由后端探测 */
export function localFileToMaterial(path: string): SelectedMaterial {
  const name = path.split(/[\\/]/).pop() || path
  return { path, name, origin: 'local', duration_seconds: null, size_bytes: 0 }
}
