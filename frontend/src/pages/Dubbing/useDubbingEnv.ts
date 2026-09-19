/**
 * 智能配音的服务环境（Voicebox 探测 + 手动指定服务地址）。
 *
 * 为什么单独一个 hook：进页面探测一次、点「重新检测」再探一次、地址不对时
 * 手动改一个 —— 这三件事共用同一份状态与同一套提示，散在页面里会是一组
 * 互相牵制的 useState + useEffect + fetch。
 *
 * 手动指定的地址由后端写进 backend/.env 并原地热更新，所以改完不需要重启
 * 服务：写接口直接把最新的自检结果返回回来，这里用 setData 塞回去，
 * 省掉一次多余的 GET（也避免中间那一帧的空窗）。
 *
 * 与 useSubtitleEnv 的一处差异：Voicebox 连不上**不是**异常状态，而是
 * 「桌面端没开」——所以提示一律走 warning 语义，页面上的 Alert 负责如实展示。
 */

import { useCallback, useEffect, useRef } from 'react'

import { fetchVoiceboxEnvironment, updateVoiceboxBaseUrl } from '../../api/voicebox'
import type { VoiceboxEnvironment } from '../../types/voicebox'
import { useAsyncData } from '../../hooks/useAsyncData'
import type { UseApiMessageResult } from '../../hooks/useApiMessage'

export interface UseDubbingEnvOptions {
  /** 提示接口，直接传 useApiMessage() 的返回值 */
  api: UseApiMessageResult
}

export interface UseDubbingEnvResult {
  /** 探测结果；首次加载完成前为 null */
  env: VoiceboxEnvironment | null
  /** 探测中（不含手动指定地址的请求） */
  loading: boolean
  /** 探测失败的原因（成功时为空串） */
  error: string
  /** 重新探测；传 true 绕过后端缓存（「重新检测」按钮用） */
  refresh: (force?: boolean) => Promise<VoiceboxEnvironment | null>
  /**
   * 指定 Voicebox 服务地址。
   *
   * @param baseUrl 服务地址；传空串表示恢复内置默认地址。
   * @returns 是否指定成功（地址非法等参数问题会失败并已提示）
   */
  setBaseUrl: (baseUrl: string) => Promise<boolean>
}

export function useDubbingEnv({ api }: UseDubbingEnvOptions): UseDubbingEnvResult {
  const { data, loading, error, reload, setData } = useAsyncData<VoiceboxEnvironment, boolean>({
    // GET /voicebox/environment 的查询参数就是 refresh，正好对上 A = boolean
    load: (force) => fetchVoiceboxEnvironment(Boolean(force)),
    failMessage: '环境探测失败',
    // 探测失败不弹提示打断页面：页面上本来就有一条说明环境状态的 Alert
    silent: true,
    fail: api.fail,
  })

  // api 每次渲染都是新对象，进 ref 保证 setBaseUrl 的引用保持稳定
  const latest = useRef(api)
  useEffect(() => {
    latest.current = api
  })

  const refresh = useCallback((force = false) => reload(force), [reload])

  const setBaseUrl = useCallback(
    async (baseUrl: string): Promise<boolean> => {
      try {
        const next = await updateVoiceboxBaseUrl(baseUrl)
        setData(next)
        notifySetResult(baseUrl, next, latest.current)
        return true
      } catch (err) {
        latest.current.fail(err, '指定服务地址失败')
        return false
      }
    },
    [setData],
  )

  return { env: data, loading, error, refresh, setBaseUrl }
}

/** 指定地址成功后的动作反馈；环境本身的状态由页面上的 Alert 如实展示 */
function notifySetResult(
  baseUrl: string,
  env: VoiceboxEnvironment,
  api: UseApiMessageResult,
): void {
  if (env.warnings.length > 0) {
    env.warnings.forEach((text) => api.message.warning(text))
  }
  if (baseUrl === '') {
    api.message.success('已恢复默认服务地址')
    return
  }
  if (env.reachable) {
    api.message.success('已指定 Voicebox 服务地址')
    return
  }
  // 地址写进去了但连不上：不说「失败」（地址本身是合法的），指向该做的事
  api.message.warning('地址已保存，但连不上 —— 请确认 Voicebox 桌面端已打开')
}
