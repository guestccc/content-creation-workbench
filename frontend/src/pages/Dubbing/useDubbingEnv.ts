/**
 * 智能配音的服务环境（Voicebox 探测 + 手动指定服务地址 + 设镜像 / 重启）。
 *
 * 为什么单独一个 hook：进页面探测一次、点「重新检测」再探一次、地址不对时
 * 手动改一个、模型下不动时设一次镜像再重启一次 —— 这几件事共用同一份状态与
 * 同一套提示，散在页面里会是一组互相牵制的 useState + useEffect + fetch。
 *
 * 手动指定的地址由后端写进 backend/.env 并原地热更新，所以改完不需要重启
 * 服务：写接口直接把最新的自检结果返回回来，这里用 setData 塞回去，
 * 省掉一次多余的 GET（也避免中间那一帧的空窗）。设镜像 / 清镜像同理。
 *
 * **重启比别的动作多一段等待**：下载源是 Voicebox 启动那一刻读进内存的，改完
 * 必须重启才生效；而冷启动到服务可用约 30 秒，后端重启接口**不等就绪**就返回
 * （前端 fetch 只有 15 秒超时）。所以那段等待的轮询写在这里 —— 页面只做编排。
 *
 * 与 useSubtitleEnv 的一处差异：Voicebox 连不上**不是**异常状态，而是
 * 「桌面端没开」——所以提示一律走 warning 语义，页面上的 Alert 负责如实展示。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import {
  disableVoiceboxHfMirror,
  enableVoiceboxHfMirror,
  fetchVoiceboxEnvironment,
  restartVoicebox,
  updateVoiceboxBaseUrl,
} from '../../api/voicebox'
import type { VoiceboxEnvironment } from '../../types/voicebox'
import { useAsyncData } from '../../hooks/useAsyncData'
import { POLL_INTERVAL_MS } from '../../hooks/useJobPolling'
import type { UseApiMessageResult } from '../../hooks/useApiMessage'

/**
 * 重启后最多盯多久。
 *
 * 实测冷启动约 30 秒；给到 60 秒是因为首次启动还要校验/加载模型、以及机器慢的
 * 情况。超时不是失败（可能只是还没起完），所以提示语引导用户点「重新检测」，
 * 而不是说「重启失败」。
 */
const READY_TIMEOUT_MS = 60_000

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
  /** 设 / 清镜像的请求进行中（按钮转圈用） */
  mirrorBusy: boolean
  /**
   * 一键把模型下载源换成镜像（写**用户级**环境变量，不是 .env）。
   *
   * @returns 是否设置成功。**成功不代表已生效**：要等用户点「重启 Voicebox」。
   */
  setMirror: () => Promise<boolean>
  /** 清除下载源、恢复 HuggingFace 官方默认（同样要重启才生效） */
  clearMirror: () => Promise<boolean>
  /** 正在重启、或重启完了还在等 Voicebox 起来 */
  restarting: boolean
  /**
   * 重启 Voicebox 桌面端。
   *
   * 接口不等就绪就返回，之后的就绪等待由本 hook 轮询（约 30 秒，不阻断页面）。
   *
   * @returns 是否已发起重启（找不到安装位置、地址是远程的都会失败并已提示）
   */
  restart: () => Promise<boolean>
}

export function useDubbingEnv({ api }: UseDubbingEnvOptions): UseDubbingEnvResult {
  const [mirrorBusy, setMirrorBusy] = useState(false)
  const [restarting, setRestarting] = useState(false)

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

  const applyMirrorChange = useCallback(
    async (
      request: () => Promise<VoiceboxEnvironment>,
      successText: string,
      failText: string,
    ): Promise<boolean> => {
      setMirrorBusy(true)
      try {
        const next = await request()
        setData(next)
        latest.current.message.success(successText)
        return true
      } catch (err) {
        latest.current.fail(err, failText)
        return false
      } finally {
        setMirrorBusy(false)
      }
    },
    [setData],
  )

  const setMirror = useCallback(
    () =>
      applyMirrorChange(
        enableVoiceboxHfMirror,
        '下载源已设为镜像，重启 Voicebox 后生效',
        '设置镜像失败',
      ),
    [applyMirrorChange],
  )

  const clearMirror = useCallback(
    () =>
      applyMirrorChange(
        disableVoiceboxHfMirror,
        '已恢复 HuggingFace 官方默认源，重启 Voicebox 后生效',
        '清除镜像失败',
      ),
    [applyMirrorChange],
  )

  // 「重启后等就绪」的定时器。放 ref 而不是 state：它不该触发重渲染，
  // 也不该进任何依赖数组（拿它当依赖会把定时器拆了重建，间隔就不再是间隔）。
  const readyTimer = useRef<number | null>(null)

  const stopReadyWatch = useCallback(() => {
    if (readyTimer.current !== null) {
      window.clearInterval(readyTimer.current)
      readyTimer.current = null
    }
  }, [])

  // 卸载时别让定时器继续打接口（用户切走页面了，没人要这个结果）
  useEffect(() => () => stopReadyWatch(), [stopReadyWatch])

  /** 重启后盯着自检接口，直到 reachable 或超时 */
  const watchReady = useCallback(() => {
    stopReadyWatch()
    const deadline = Date.now() + READY_TIMEOUT_MS
    // 间隔与任务轮询取同一个常量：页面节奏一致，也不必再记一个魔法数字
    readyTimer.current = window.setInterval(() => {
      void (async () => {
        if (Date.now() > deadline) {
          stopReadyWatch()
          setRestarting(false)
          latest.current.message.warning(
            '等了一分钟还没等到 Voicebox 就绪：它可能还在加载模型，点「重新检测」看看；' +
              '一直不行就手动打开 Voicebox。',
          )
          return
        }
        try {
          // refresh=true 绕过后端缓存：启动这段时间缓存里那份永远是「连不上」
          const next = await fetchVoiceboxEnvironment(true)
          setData(next)
          if (next.reachable) {
            stopReadyWatch()
            setRestarting(false)
            latest.current.message.success('Voicebox 已就绪')
          }
        } catch {
          // 启动过程中单次探测失败很正常，下个周期会重试
        }
      })()
    }, POLL_INTERVAL_MS)
  }, [setData, stopReadyWatch])

  const restart = useCallback(async (): Promise<boolean> => {
    setRestarting(true)
    try {
      const result = await restartVoicebox()
      latest.current.message.success(result.detail || '已发起重启，正在等 Voicebox 起来')
      watchReady()
      return true
    } catch (err) {
      // 没发起成功就别停在「正在启动」上 —— 那会让页面一直抑制着真正的原因
      setRestarting(false)
      latest.current.fail(err, '重启 Voicebox 失败')
      return false
    }
  }, [watchReady])

  return {
    env: data,
    loading,
    error,
    refresh,
    setBaseUrl,
    mirrorBusy,
    setMirror,
    clearMirror,
    restarting,
    restart,
  }
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
