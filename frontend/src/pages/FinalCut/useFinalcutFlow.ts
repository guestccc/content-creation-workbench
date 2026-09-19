/**
 * 一键成品页面的主流程状态（单页私有，不进公共 hooks）。
 *
 * 三步状态机：① 选素材（字幕 + 成片）→ ② AI 文案（生成 / 勾选 / 改字）→
 * ③ 框选与合成。文案任务的生命周期复用公共的 useJobRunner；候选文案的
 * 勾选状态、用户改过的字、每条的框/样式/字号都在这里 —— 页面只编排。
 */

import { useCallback, useMemo, useRef, useState } from 'react'
import { useEffect } from 'react'

import {
  batchDeleteCopyJobs,
  batchDeleteRenderJobs,
  cancelCopyJob,
  cancelRenderJob,
  createCopyJob,
  createRenderJob,
  deleteCopyJob,
  deleteRenderJob,
  fetchCopyJob,
  fetchFinalcutSources,
  fetchRenderJob,
} from '../../api/finalcut'
import { useJobRunner } from '../../hooks/useJobRunner'
import type {
  BoxSpec,
  CopyJobPayload,
  FinalcutCopyJob,
  FinalcutRenderJob,
  FinalcutSources,
  RenderJobPayload,
  TextStyleKey,
} from '../../types/finalcut'
import { isTerminalStatus } from '../../types/finalcut'
import type { UseApiMessageResult } from '../../hooks/useApiMessage'
import type { UsePurgeFilesResult } from '../../hooks/usePurgeFiles'

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

/** 一条候选文案在页面上的状态（AI 产物 + 用户的勾选与改动） */
export interface CandidateState {
  /** 对应 copyJob.result.copies 的下标 */
  key: number
  checked: boolean
  /** 文案正文（用户可改字；初始为 AI 给的原文） */
  text: string
  /** 框选区域（归一化）；boxSet=false 时这只是默认值，第三步要真框一次 */
  box: BoxSpec
  /** 是否已经在视频上框过位置（第三步的完成标记） */
  boxSet: boolean
  style: TextStyleKey
  /** 0 = 自动字号 */
  fontSize: number
}

/** 新候选的默认框：画面下方居中的一条横带（带货文案最常见的位置） */
const DEFAULT_BOX: BoxSpec = { x: 0.1, y: 0.72, w: 0.8, h: 0.16 }

export interface UseFinalcutFlowResult {
  /** 当前在第几步（0 选素材 / 1 AI 文案 / 2 框选与合成） */
  step: number
  subtitle: SelectedMaterial | null
  video: SelectedMaterial | null
  /** 产物目录（空串 = 后端默认 materials/finalcut/） */
  outputDir: string
  selectSubtitle: (item: SelectedMaterial) => void
  selectVideo: (item: SelectedMaterial) => void
  clearSubtitle: () => void
  clearVideo: () => void
  setOutputDir: (dir: string) => void
  /** 环境自检回填默认产物目录（用户没手选过才生效） */
  fillDefaultOutputDir: (dir: string) => void
  /** 第 ① 步的校验：空串表示可以生成文案 */
  validationError: string
  /** 历史产物清单（选素材弹窗用） */
  sources: FinalcutSources | null
  sourcesLoading: boolean
  loadSources: () => Promise<void>

  /** 开始生成文案（建 copy job 并进入第 ② 步）；已在跑时会先被拒 */
  startCopyJob: () => Promise<void>
  /** 回到上一步（状态保留，素材与勾选不清空） */
  goBack: () => void
  copyJob: FinalcutCopyJob | null
  copyJobRunning: boolean
  copyJobSubmitting: boolean
  cancelCurrentCopyJob: () => Promise<void>
  /** 「换一批」：同素材再建一个文案任务 */
  regenerate: () => Promise<void>

  /** 候选文案（AI 产物 + 勾选状态 + 用户改过的字） */
  candidates: CandidateState[]
  toggleCandidate: (key: number) => void
  updateCandidateText: (key: number, text: string) => void
  updateCandidateStyle: (key: number, style: TextStyleKey) => void
  updateCandidateFontSize: (key: number, fontSize: number) => void
  updateCandidateBox: (key: number, box: BoxSpec) => void
  /** 把某条的样式/字号套到全部候选（同一条视频通常统一风格） */
  applyStyleToAll: (style: TextStyleKey, fontSize: number) => void
  /** 勾选的候选数（进入第 ③ 步的门槛） */
  checkedCount: number
  /** 进入第 ③ 步（至少勾一条才放行，由页面先校验 checkedCount） */
  goToBoxStep: () => void
  /** 从历史列表点开一条文案任务：回填候选并进入第 ② 步 */
  openCopyJob: (jobId: number) => Promise<void>
  /** 删除文案任务记录（磁盘无产物，只删记录） */
  removeCopyJob: (jobId: number) => Promise<boolean>
  /** 批量删除文案任务记录 */
  removeCopyJobs: (ids: number[]) => Promise<boolean>
  /** 取消任意一条文案任务（历史列表用），返回更新后的任务 */
  cancelCopyJobById: (jobId: number) => Promise<FinalcutCopyJob | null>

  /** 开始合成：勾选的候选一条一个 item，提交合成任务 */
  startRenderJob: () => Promise<void>
  renderJob: FinalcutRenderJob | null
  renderJobRunning: boolean
  renderJobSubmitting: boolean
  cancelCurrentRenderJob: () => Promise<void>
  /** 取消任意一条合成任务（历史列表用） */
  cancelRenderJobById: (jobId: number) => Promise<FinalcutRenderJob | null>
  /** 读一条合成任务详情（历史「查看」用，不动当前任务） */
  readRenderJob: (jobId: number) => Promise<FinalcutRenderJob | null>
  /** 删除合成任务记录（purge 勾选由页面的 usePurgeFiles 经 options 传入） */
  removeRenderJob: (jobId: number) => Promise<boolean>
  removeRenderJobs: (ids: number[]) => Promise<boolean>
}

export interface UseFinalcutFlowOptions {
  /** 文案任务有增删改时回调（页面拿它刷新历史列表） */
  onCopyJobChanged?: () => void
  /** 合成任务有增删改时回调（页面拿它刷新合成历史列表） */
  onRenderJobChanged?: () => void
  /** 合成任务删除确认框的 purge 勾选（文案任务磁盘无产物，不需要） */
  purge?: UsePurgeFilesResult
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
  const [outputDir, setOutputDirState] = useState('')
  const [outputDirTouched, setOutputDirTouched] = useState(false)

  const [sources, setSources] = useState<FinalcutSources | null>(null)
  const [sourcesLoading, setSourcesLoading] = useState(false)

  const selectSubtitle = useCallback((item: SelectedMaterial) => setSubtitle(item), [])
  const selectVideo = useCallback((item: SelectedMaterial) => setVideo(item), [])
  const clearSubtitle = useCallback(() => setSubtitle(null), [])
  const clearVideo = useCallback(() => setVideo(null), [])

  const setOutputDir = useCallback((dir: string) => {
    setOutputDirTouched(true)
    setOutputDirState(dir)
  }, [])

  // 用户没手选过才回填默认值：环境异步到达不能盖掉用户已经选好的目录
  const fillDefaultOutputDir = useCallback((dir: string) => {
    setOutputDirState((current) => (current || outputDirTouched ? current : dir))
  }, [outputDirTouched])

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
  // 第 ② 步：AI 文案任务生命周期 + 候选勾选
  // ------------------------------------------------------------------
  const [candidates, setCandidates] = useState<CandidateState[]>([])

  // api 进 ref，保证下面的回调引用恒定（useJobRunner 也走同一套「最新 ref」）
  const apiRef = useRef(api)
  useEffect(() => {
    apiRef.current = api
  })

  /** 按任务结果重建候选列表（跑完回填 / 从历史点开 共用） */
  const rebuildCandidates = useCallback((job: FinalcutCopyJob) => {
    const copies = job.status === 'success' ? (job.result?.copies ?? []) : []
    setCandidates(
      copies.map((copy, index) => ({
        key: index,
        checked: false,
        text: copy.text,
        box: DEFAULT_BOX,
        boxSet: false,
        style: 'white_box' as TextStyleKey,
        fontSize: 0,
      })),
    )
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
    const created = await copyRunner.submit({
      subtitle_path: subtitle.path,
      video_path: video.path,
    })
    if (created) {
      setCandidates([])
      setStep(1)
    }
  }, [subtitle, video, copyRunner])

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

  const goBack = useCallback(() => {
    setStep((current) => Math.max(0, current - 1))
  }, [])

  const goToBoxStep = useCallback(() => {
    setStep(2)
  }, [])

  // ------------------------------------------------------------------
  // 候选文案的勾选与编辑
  // ------------------------------------------------------------------
  const patchCandidate = useCallback((key: number, patch: Partial<CandidateState>) => {
    setCandidates((list) =>
      list.map((item) => (item.key === key ? { ...item, ...patch } : item)),
    )
  }, [])

  const toggleCandidate = useCallback(
    (key: number) => {
      setCandidates((list) =>
        list.map((item) => (item.key === key ? { ...item, checked: !item.checked } : item)),
      )
    },
    [],
  )

  const updateCandidateText = useCallback(
    (key: number, text: string) => patchCandidate(key, { text }),
    [patchCandidate],
  )
  const updateCandidateStyle = useCallback(
    (key: number, style: TextStyleKey) => patchCandidate(key, { style }),
    [patchCandidate],
  )
  const updateCandidateFontSize = useCallback(
    (key: number, fontSize: number) => patchCandidate(key, { fontSize }),
    [patchCandidate],
  )
  const updateCandidateBox = useCallback(
    (key: number, box: BoxSpec) => patchCandidate(key, { box, boxSet: true }),
    [patchCandidate],
  )
  const applyStyleToAll = useCallback((style: TextStyleKey, fontSize: number) => {
    setCandidates((list) => list.map((item) => ({ ...item, style, fontSize })))
  }, [])

  const checkedCount = useMemo(
    () => candidates.filter((item) => item.checked).length,
    [candidates],
  )

  // ------------------------------------------------------------------
  // 第 ③ 步：合成任务生命周期（勾选的候选一条一个成片）
  // ------------------------------------------------------------------
  const renderRunner = useJobRunner<FinalcutRenderJob, RenderJobPayload>({
    create: createRenderJob,
    cancel: cancelRenderJob,
    remove: (jobId) => deleteRenderJob(jobId, options?.purge?.take() ?? false),
    batchRemove: (ids) => batchDeleteRenderJobs(ids, options?.purge?.take() ?? false),
    fetchJob: fetchRenderJob,
    isTerminal: (job) => isTerminalStatus(job.status),
    fail: api.fail,
    onChanged: options?.onRenderJobChanged,
    onFinished: (job) => {
      if (job.status === 'success' || job.status === 'partial') {
        apiRef.current.message.success(
          `合成完成：${job.completed_items}/${job.total_items} 条成片`,
        )
      }
    },
  })

  const startRenderJob = useCallback(async () => {
    if (!video || renderRunner.running) {
      return
    }
    const copies = copyRunner.job?.result?.copies ?? []
    const items = candidates
      .filter((item) => item.checked)
      .map((item) => ({
        copy_text: item.text,
        angle: copies[item.key]?.angle || undefined,
        style: item.style,
        box: item.box,
        font_size: item.fontSize,
      }))
    if (items.length === 0) {
      return
    }
    await renderRunner.submit({
      video_path: video.path,
      copy_job_id: copyRunner.job?.id ?? undefined,
      items,
      output_dir: outputDir || undefined,
    })
  }, [video, candidates, copyRunner.job, outputDir, renderRunner])

  const cancelCurrentRenderJob = useCallback(async () => {
    if (renderRunner.job) {
      await renderRunner.cancel(renderRunner.job.id)
    }
  }, [renderRunner])

  const cancelRenderJobById = useCallback(
    (jobId: number) => renderRunner.cancel(jobId),
    [renderRunner],
  )
  const readRenderJob = useCallback(
    (jobId: number) => renderRunner.read(jobId),
    [renderRunner],
  )
  const removeRenderJob = useCallback(
    (jobId: number) => renderRunner.remove(jobId),
    [renderRunner],
  )
  const removeRenderJobs = useCallback(
    (ids: number[]) => renderRunner.removeMany(ids),
    [renderRunner],
  )

  return {
    step,
    subtitle,
    video,
    outputDir,
    selectSubtitle,
    selectVideo,
    clearSubtitle,
    clearVideo,
    setOutputDir,
    fillDefaultOutputDir,
    validationError,
    sources,
    sourcesLoading,
    loadSources,
    startCopyJob,
    goBack,
    copyJob: copyRunner.job,
    copyJobRunning: copyRunner.running,
    copyJobSubmitting: copyRunner.submitting,
    cancelCurrentCopyJob,
    regenerate,
    candidates,
    toggleCandidate,
    updateCandidateText,
    updateCandidateStyle,
    updateCandidateFontSize,
    updateCandidateBox,
    applyStyleToAll,
    checkedCount,
    goToBoxStep,
    openCopyJob,
    removeCopyJob,
    removeCopyJobs,
    cancelCopyJobById,
    startRenderJob,
    renderJob: renderRunner.job,
    renderJobRunning: renderRunner.running,
    renderJobSubmitting: renderRunner.submitting,
    cancelCurrentRenderJob,
    cancelRenderJobById,
    readRenderJob,
    removeRenderJob,
    removeRenderJobs,
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
