/**
 * 字幕提取的运行环境（VideoCaptioner / ffmpeg 探测 + 手动指定安装目录）。
 *
 * 为什么单独一个 hook：进页面探测一次、点「重新检测」再探一次、猜不中时
 * 手动指一个目录 —— 这三件事共用同一份状态与同一套提示，散在页面里会是
 * 一组互相牵制的 useState + useEffect + fetch。
 *
 * 手动指定的目录由后端写进 backend/.env 并原地热更新，所以选定后不需要
 * 重启服务：写接口直接把最新的自检结果返回回来，这里用 setData 塞回去，
 * 省掉一次多余的 GET（也避免中间那一帧的空窗）。
 */

import { useCallback, useEffect, useRef } from 'react'

import { fetchSubtitleEnvironment, updateSubtitleVcRoot } from '../../api/subtitle'
import type { SubtitleEnvironment } from '../../types/subtitle'
import { useAsyncData } from '../../hooks/useAsyncData'
import type { UseApiMessageResult } from '../../hooks/useApiMessage'

export interface UseSubtitleEnvOptions {
  /** 提示接口，直接传 useApiMessage() 的返回值 */
  api: UseApiMessageResult
  /** 探测成功后的回调，页面用它回填默认的输入/输出目录 */
  onLoaded?: (env: SubtitleEnvironment) => void
}

export interface UseSubtitleEnvResult {
  /** 探测结果；首次加载完成前为 null */
  env: SubtitleEnvironment | null
  /** 探测中（不含手动指定目录的请求） */
  loading: boolean
  /** 探测失败的原因（成功时为空串；页面用它渲染环境 Alert 的兜底态） */
  error: string
  /** 重新探测；传 true 绕过后端缓存（「重新检测」按钮用） */
  refresh: (force?: boolean) => Promise<SubtitleEnvironment | null>
  /**
   * 指定 VideoCaptioner 的安装目录。
   *
   * @param path 绝对路径；传空串表示清除指定、恢复自动探测。
   * @returns 是否指定成功（目录不存在等参数问题会失败并已提示）
   */
  setVcRoot: (path: string) => Promise<boolean>
}

export function useSubtitleEnv({ api, onLoaded }: UseSubtitleEnvOptions): UseSubtitleEnvResult {
  const { data, loading, error, reload, setData } = useAsyncData<
    SubtitleEnvironment,
    boolean
  >({
    // GET /subtitle/environment 的查询参数就是 refresh，正好对上 A = boolean
    load: (force) => fetchSubtitleEnvironment(Boolean(force)),
    failMessage: '环境探测失败',
    // 探测失败不弹提示打断页面：页面上本来就有一条说明环境状态的 Alert
    silent: true,
    fail: api.fail,
    onLoaded,
  })

  // api 每次渲染都是新对象，进 ref 保证 setVcRoot 的引用保持稳定
  const latest = useRef(api)
  useEffect(() => {
    latest.current = api
  })

  const refresh = useCallback((force = false) => reload(force), [reload])

  const setVcRoot = useCallback(
    async (path: string): Promise<boolean> => {
      try {
        const next = await updateSubtitleVcRoot(path)
        setData(next)
        notifySetResult(path, next, latest.current.message)
        return true
      } catch (err) {
        latest.current.fail(err, '指定目录失败')
        return false
      }
    },
    [setData],
  )

  return { env: data, loading, error, refresh, setVcRoot }
}

/** 指定目录成功后的动作反馈；环境本身的状态由页面上的 Alert 如实展示 */
function notifySetResult(
  path: string,
  env: SubtitleEnvironment,
  message: UseApiMessageResult['message'],
): void {
  if (env.warnings.length > 0) {
    env.warnings.forEach((text) => message.warning(text))
    return
  }
  if (path === '') {
    message.success('已恢复自动探测')
  } else if (env.installed) {
    message.success('已指定 VideoCaptioner 目录')
  } else {
    message.warning('已指定目录，但未在其中检测到可用的 VideoCaptioner')
  }
}
