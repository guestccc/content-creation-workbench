/**
 * 智能配音（Voicebox）相关的接口调用。
 *
 * 每个函数对应后端一个接口，返回值已完成拆包（直接拿到 data）。
 *
 * 注意生成接口的形态：`createGeneration` 入队后**立即**返回（后端 worker 在
 * 后台跑，Voicebox 的生成是同步阻塞的，长文案要几分钟），进度靠
 * `fetchGeneration` 轮询 —— 与切割/字幕同一套「任务异步跑、进度靠轮询」。
 */

import { del, get, post, put } from './client'
import type {
  DubbingAudioListData,
  DubbingGeneration,
  DubbingGenerationPayload,
  VoiceProfileListData,
  VoiceboxEnvironment,
} from '../types/voicebox'

/** 探测 Voicebox 服务：连没连上、模型下没下、有没有 GPU（refresh 绕过后端缓存） */
export function fetchVoiceboxEnvironment(refresh = false): Promise<VoiceboxEnvironment> {
  return get<VoiceboxEnvironment>(
    '/voicebox/environment',
    refresh ? { refresh: true } : undefined,
  )
}

/**
 * 指定 Voicebox 服务地址（空串表示恢复默认地址）。
 *
 * 后端会把地址写进 backend/.env 并原地热更新，返回值就是最新的自检结果，
 * 调用方直接拿它渲染，不必再发一次 GET。
 */
export function updateVoiceboxBaseUrl(baseUrl: string): Promise<VoiceboxEnvironment> {
  return put<VoiceboxEnvironment>('/voicebox/environment/base-url', { base_url: baseUrl })
}

/** 拉音色列表（建音色在 Voicebox 自己的界面里做） */
export function fetchVoiceProfiles(): Promise<VoiceProfileListData> {
  return get<VoiceProfileListData>('/voicebox/profiles')
}

/** 提交一次配音生成（入队后立即返回，进度靠 fetchGeneration 轮询） */
export function createGeneration(
  payload: DubbingGenerationPayload,
): Promise<DubbingGeneration> {
  return post<DubbingGeneration>('/voicebox/generations', payload)
}

/** 查询单次生成的进度 */
export function fetchGeneration(generationId: number): Promise<DubbingGeneration> {
  return get<DubbingGeneration>(`/voicebox/generations/${generationId}`)
}

/** 配音产物清单（materials/dubbing/ 扫盘 + 索引，重启后依然在） */
export function fetchDubbingAudios(): Promise<DubbingAudioListData> {
  return get<DubbingAudioListData>('/voicebox/audios')
}

/** 删除一份配音产物（文件 + 索引条目一起删） */
export function deleteDubbingAudio(name: string): Promise<{ name: string }> {
  return del<{ name: string }>(`/voicebox/audios/${encodeURIComponent(name)}`)
}
