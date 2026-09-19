/**
 * 智能配音页的运行态：音色列表 + 提交生成 + 轮询进度 + 产物清单 + 删除。
 *
 * 为什么单独一个 hook：这五件事共用同一份状态（当前生成、产物列表）与同一套
 * 提示，而且彼此有因果关系（生成成功 → 刷新产物清单），散在页面里就是一组
 * 互相牵制的 useState + useEffect + fetch。
 *
 * 轮询直接用公用的 useJobPolling：后端把生成记录设计成 `{ id: number }` 的形态，
 * 正是为了复用这套「任务异步跑、进度靠轮询」的现成逻辑（前端 fetch 超时只有
 * 15 秒，而 Voicebox 的生成是同步阻塞的，长文案要几分钟）。
 */

import { useCallback, useEffect, useState } from 'react'

import {
  createGeneration,
  deleteDubbingAudio,
  fetchDubbingAudios,
  fetchGeneration,
  fetchVoiceProfiles,
} from '../../api/voicebox'
import type {
  DubbingAudio,
  DubbingGeneration,
  DubbingGenerationPayload,
  VoiceProfile,
} from '../../types/voicebox'
import { isGenerationTerminal } from '../../types/voicebox'
import { useJobPolling } from '../../hooks/useJobPolling'
import type { UseApiMessageResult } from '../../hooks/useApiMessage'
import { formatDuration } from '../../utils/format'

export interface UseDubbingOptions {
  /** 提示接口，直接传 useApiMessage() 的返回值 */
  api: UseApiMessageResult
}

export interface UseDubbingResult {
  /** Voicebox 里的音色（读不到时为空数组，页面负责给引导文案） */
  profiles: VoiceProfile[]
  profilesLoading: boolean
  /** 重新拉音色（「重新检测」时顺带拉一次） */
  reloadProfiles: () => Promise<void>
  /** 产物清单（磁盘 + 索引，重启后端后依然在） */
  audios: DubbingAudio[]
  audiosLoading: boolean
  reloadAudios: () => Promise<void>
  /** 当前这次生成（null 表示页面打开后还没提交过） */
  generation: DubbingGeneration | null
  /** 生成还在跑（按钮禁用与进度展示用） */
  running: boolean
  /** 提交中（禁用按钮防连点） */
  submitting: boolean
  /** 提交一次生成；成功返回记录，失败返回 null（提示已发） */
  submit: (payload: DubbingGenerationPayload) => Promise<DubbingGeneration | null>
  /** 删掉一份产物（文件 + 索引条目） */
  removeAudio: (name: string) => Promise<boolean>
}

export function useDubbing({ api }: UseDubbingOptions): UseDubbingResult {
  const [profiles, setProfiles] = useState<VoiceProfile[]>([])
  const [profilesLoading, setProfilesLoading] = useState(false)
  const [audios, setAudios] = useState<DubbingAudio[]>([])
  const [audiosLoading, setAudiosLoading] = useState(false)
  const [generation, setGeneration] = useState<DubbingGeneration | null>(null)
  const [submitting, setSubmitting] = useState(false)

  // api 每次渲染都是新对象，进 ref 让下面的回调引用保持稳定
  const fail = api.fail

  /** 拉音色列表。失败**不弹提示**：服务没开这件事由环境 Alert 说，这里再弹一次
   *  就是同一件事说两遍 —— 页面上的 Select 空态会指向该做什么。 */
  const reloadProfiles = useCallback(async () => {
    setProfilesLoading(true)
    try {
      const data = await fetchVoiceProfiles()
      setProfiles(data.items)
    } catch {
      setProfiles([])
    } finally {
      setProfilesLoading(false)
    }
  }, [])

  const reloadAudios = useCallback(async () => {
    setAudiosLoading(true)
    try {
      const data = await fetchDubbingAudios()
      setAudios(data.items)
    } catch (err) {
      fail(err, '读取配音产物失败')
    } finally {
      setAudiosLoading(false)
    }
  }, [fail])

  // 进页面拉一次音色与产物清单
  useEffect(() => {
    void reloadProfiles()
    void reloadAudios()
  }, [reloadProfiles, reloadAudios])

  // 当前这次生成还在跑时轮询。依赖只跟「哪条记录、是否在跑」绑定（useJobPolling
  // 内部处理的），拿到新进度写回 state 不会把定时器拆了重建。
  useJobPolling<DubbingGeneration>({
    job: generation,
    fetchJob: fetchGeneration,
    isTerminal: (item) => isGenerationTerminal(item.status),
    onUpdate: setGeneration,
    onFinish: (item) => {
      if (item.status === 'success') {
        api.message.success(`配音已生成（${formatDuration(item.duration)}）`)
        void reloadAudios()
      }
      // 失败不弹提示：页面上那条 Alert 会显示后端给的原因，不必说两遍
    },
  })

  const submit = useCallback(
    async (payload: DubbingGenerationPayload): Promise<DubbingGeneration | null> => {
      setSubmitting(true)
      try {
        const created = await createGeneration(payload)
        setGeneration(created)
        return created
      } catch (err) {
        fail(err, '提交配音生成失败')
        return null
      } finally {
        setSubmitting(false)
      }
    },
    [fail],
  )

  const removeAudio = useCallback(
    async (name: string): Promise<boolean> => {
      try {
        await deleteDubbingAudio(name)
        api.message.success('已删除配音产物')
        void reloadAudios()
        return true
      } catch (err) {
        fail(err, '删除配音产物失败')
        return false
      }
    },
    [api, fail, reloadAudios],
  )

  return {
    profiles,
    profilesLoading,
    reloadProfiles,
    audios,
    audiosLoading,
    reloadAudios,
    generation,
    running: generation !== null && !isGenerationTerminal(generation.status),
    submitting,
    submit,
    removeAudio,
  }
}
