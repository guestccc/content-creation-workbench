/**
 * 「AI 文案」弹窗的状态机（本页私有，不进公共 hooks）。
 *
 * 一条笔记一套流程：打开 → 先读落库结果 → 没有就当场生成（打开即生成）；
 * 「换一批」是覆盖式的重新生成，后端同一（任务, 笔记）只留最新一份。
 *
 * 生成走 SSE（后端 /ai-copies/stream）：思维链边到边推进 `thinking`，
 * 弹窗里的 <Think> 因此是逐字长出来的，而不是转几十秒圈再整块出现。
 *
 * 三个细节值得说明：
 *
 * 1. **过期帧要丢**（seqRef）：AI 调用要十几秒，这期间用户完全可能关掉弹窗、
 *    或换一条笔记再点开。帧/响应回来时先比对序号，不是当前这一轮的不许写状态
 *    —— 否则会给 B 笔记的弹窗塞进 A 笔记的思维链。close() 也把序号加一。
 * 2. **在途请求要掐断**（abortRef）：关弹窗、换一批、换一条笔记都调
 *    cancelInFlight()。abort 不是瞬时的，掐断之后可能还有一两帧在路上，所以
 *    序号仍是最终判据 —— 两者是「省后端算力」与「不写脏状态」两件事，都要。
 * 3. **失败不弹 toast，摆在弹窗里**：失败原因要配一个动作（「去配置」或
 *    「重试」），而 toast 一闪而过点不着。所以这个 hook 不要 fail 回调。
 */

import { useCallback, useRef, useState } from 'react'

import { fetchNoteAiCopy, streamNoteAiCopy } from '../../api/crawler'
import { describeError } from '../../api/client'
import type { CrawlNote, CrawlNoteAiCopy } from '../../types/crawler'

/**
 * 失败的两类分法，决定弹窗里给哪个动作：
 * - `config`：key 没配 / 配错了 → 给「去配置」，保存后自动重试
 * - `generate`：模型这轮没答好、网络抖了 → 给「重试」（换一批）
 */
export type AiCopyErrorKind = 'config' | 'generate'

/** 后端把「没配置」和「key 不对」分成两个码，前端归成一类处理 */
const CONFIG_ERROR_CODES = ['AI_NOT_CONFIGURED', 'AI_AUTH_FAILED']

interface AiCopyTarget {
  jobId: number
  /** 正在给哪条笔记生成；弹窗标题要用它的标题 */
  note: CrawlNote
}

export interface UseAiCopyResult {
  /** 弹窗是否开着、开在哪条笔记上；null = 关着 */
  target: AiCopyTarget | null
  /** 生成结果；null 表示还没拿到（生成中或失败了） */
  copy: CrawlNoteAiCopy | null
  /** 这一轮的思维链（生成中是实时累积的，回看时是落库的原文；空串 = 没有） */
  thinking: string
  generating: boolean
  error: string
  errorKind: AiCopyErrorKind
  /** 打开某条笔记的文案弹窗（已有结果直接显示，没有则自动生成） */
  open: (jobId: number, note: CrawlNote) => void
  /** 换一批：重新调 AI 覆盖当前结果；生成期间点不动 */
  regenerate: () => void
  close: () => void
}

/** 我们自己取消的（关弹窗 / 换一批）：不是错误，不写任何状态 */
function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

export function useAiCopy(): UseAiCopyResult {
  const [target, setTarget] = useState<AiCopyTarget | null>(null)
  const [copy, setCopy] = useState<CrawlNoteAiCopy | null>(null)
  const [thinking, setThinking] = useState('')
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState('')
  const [errorKind, setErrorKind] = useState<AiCopyErrorKind>('generate')

  /** 当前这一轮的序号；帧回来时对不上就丢弃（见文件头注释第 1 条） */
  const seqRef = useRef(0)
  /** 当前这一轮的取消器（见文件头注释第 2 条） */
  const abortRef = useRef<AbortController | null>(null)

  /** 掐断在途的那一轮（关弹窗 / 换一批 / 换一条笔记都要先做） */
  const cancelInFlight = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
  }, [])

  /** 跑一轮流式生成，把帧喂进状态。seq 是发起时的轮次号。 */
  const runStream = useCallback(async (jobId: number, noteId: string, seq: number) => {
    setGenerating(true)
    setError('')
    setThinking('')

    const controller = new AbortController()
    abortRef.current = controller
    try {
      await streamNoteAiCopy(
        jobId,
        noteId,
        (event, data) => {
          if (seq !== seqRef.current) {
            return
          }
          if (event === 'reasoning') {
            setThinking((prev) => prev + String(data.text ?? ''))
          } else if (event === 'done') {
            // 思维链不覆盖成 result.reasoning：实时累积的那份与落库的同一来源，
            // 覆盖只会在末尾制造一次「文字跳一下」
            setCopy(data.result as CrawlNoteAiCopy)
          } else if (event === 'error') {
            const code = String(data.code ?? '')
            setErrorKind(CONFIG_ERROR_CODES.includes(code) ? 'config' : 'generate')
            setError(String(data.message ?? '') || '生成 AI 文案失败')
          }
        },
        controller.signal,
      )
    } catch (err) {
      if (isAbortError(err) || seq !== seqRef.current) {
        return
      }
      // 连不上、或预检失败（任务/笔记不在了）：都归到「重试」
      setErrorKind('generate')
      setError(describeError(err, '生成 AI 文案失败'))
    } finally {
      if (seq === seqRef.current) {
        setGenerating(false)
        abortRef.current = null
      }
    }
  }, [])

  const open = useCallback(
    async (jobId: number, note: CrawlNote) => {
      cancelInFlight()
      const seq = seqRef.current + 1
      seqRef.current = seq
      // 先把弹窗支起来（转圈），别等读完库再开 —— 那会有一瞬间的「点了没反应」
      setTarget({ jobId, note })
      setCopy(null)
      setThinking('')
      setError('')
      setGenerating(true)
      try {
        const data = await fetchNoteAiCopy(jobId, note.id)
        if (seq !== seqRef.current) {
          return
        }
        if (data.found && data.result) {
          setCopy(data.result)
          // 回看场景也能看到思维链；老行是空串，弹窗整块不渲染
          setThinking(data.result.reasoning)
          setGenerating(false)
          return
        }
        // 没生成过：打开即生成（用户点这个按钮就是要文案，不该再点一次）
        await runStream(jobId, note.id, seq)
      } catch (err) {
        if (isAbortError(err) || seq !== seqRef.current) {
          return
        }
        // 读库失败也归到「重试」：再点一次要么读到要么读到错误
        setErrorKind('generate')
        setError(describeError(err, '读取 AI 文案失败'))
        setGenerating(false)
      }
    },
    [cancelInFlight, runStream],
  )

  const regenerate = useCallback(() => {
    if (target === null || generating) {
      return
    }
    cancelInFlight()
    // 序号加一：万一有上一次的在途帧，让它作废
    const seq = seqRef.current + 1
    seqRef.current = seq
    void runStream(target.jobId, target.note.id, seq)
  }, [target, generating, cancelInFlight, runStream])

  const close = useCallback(() => {
    // 掐断 + 序号加一：前者省后端算力，后者保证迟到的帧写不进来
    cancelInFlight()
    seqRef.current += 1
    setTarget(null)
    setCopy(null)
    setThinking('')
    setGenerating(false)
    setError('')
  }, [cancelInFlight])

  return {
    target,
    copy,
    thinking,
    generating,
    error,
    errorKind,
    open,
    regenerate,
    close,
  }
}
