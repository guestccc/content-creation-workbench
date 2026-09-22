/**
 * 一键成品的运行环境（AI 配置 / ffmpeg-drawtext / 中文字体探测）。
 *
 * 为什么单独一个 hook：进页面探测一次、点「重新检测」再探一次、保存完
 * AI 配置又要立刻看到结果 —— 这三处共用同一份状态，散在页面里会是一组
 * 互相牵制的 useState + useEffect + fetch。
 *
 * 与 useSubtitleEnv 的差别：这里没有「手动指定目录」的写接口，AI 配置的
 * 保存走 AiSettingsModal，保存成功后调 refresh(true) 重新探测即可
 * （refresh=true 会让后端先把 .env 热同步进 settings 再探测）。
 */

import { useCallback } from 'react'

import { fetchFinalcutEnvironment } from '../../api/finalcut'
import type { FinalcutEnvironment } from '../../types/finalcut'
import { useAsyncData } from '../../hooks/useAsyncData'
import type { UseApiMessageResult } from '../../hooks/useApiMessage'

export interface UseFinalcutEnvOptions {
  /** 提示接口，直接传 useApiMessage() 的返回值 */
  api: UseApiMessageResult
  /**
   * 探测成功后的回调（可选）。
   *
   * 目前没有调用方在用：它原本是页面回填「默认产物目录」的入口，烧录从页面
   * 摘掉后那个目录控件也没了。保留是为了不把 useAsyncData 的通用能力砍掉。
   */
  onLoaded?: (env: FinalcutEnvironment) => void
}

export interface UseFinalcutEnvResult {
  /** 探测结果；首次加载完成前为 null */
  env: FinalcutEnvironment | null
  /** 探测中 */
  loading: boolean
  /** 探测失败的原因（成功时为空串；页面用它渲染环境 Alert 的兜底态） */
  error: string
  /** 重新探测；传 true 让后端先热同步 .env（「重新检测」与保存配置后用） */
  refresh: (force?: boolean) => Promise<FinalcutEnvironment | null>
}

export function useFinalcutEnv({ api, onLoaded }: UseFinalcutEnvOptions): UseFinalcutEnvResult {
  const { data, loading, error, reload } = useAsyncData<FinalcutEnvironment, boolean>({
    load: (force) => fetchFinalcutEnvironment(Boolean(force)),
    failMessage: '环境探测失败',
    // 探测失败不弹提示打断页面：页面上本来就有一条说明环境状态的 Alert
    silent: true,
    fail: api.fail,
    onLoaded,
  })

  const refresh = useCallback((force = false) => reload(force), [reload])

  return { env: data, loading, error, refresh }
}
