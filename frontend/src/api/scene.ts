/**
 * 智能镜头分割相关的接口调用。
 *
 * 每个函数对应后端一个接口，返回值已完成拆包（直接拿到 data）。
 */

import { del, get, post } from './client'
import type {
  SceneClip,
  SceneEnvironment,
  SceneJob,
  SceneJobListData,
  SceneJobPayload,
  SceneSummary,
  SceneTemplate,
} from '../types/scene'

/** 获取全部预设模板（含「自定义」占位项） */
export function fetchSceneTemplates(): Promise<SceneTemplate[]> {
  return get<SceneTemplate[]>('/scene/templates')
}

/** 探测 vct / scenedetect / ffmpeg 是否可用 */
export function fetchSceneEnvironment(): Promise<SceneEnvironment> {
  return get<SceneEnvironment>('/scene/environment')
}

/** 创建镜头分割任务 */
export function createSceneJob(payload: SceneJobPayload): Promise<SceneJob> {
  return post<SceneJob>('/scene/jobs', payload)
}

/** 分页查询历史任务（不返回每个视频的明细） */
export function fetchSceneJobs(params: {
  page?: number
  page_size?: number
  status?: string
  mode?: string
}): Promise<SceneJobListData> {
  return get<SceneJobListData>('/scene/jobs', { ...params })
}

/** 获取任务详情（轮询进度也用它） */
export function fetchSceneJob(jobId: number): Promise<SceneJob> {
  return get<SceneJob>(`/scene/jobs/${jobId}`)
}

/** 获取任务的切点汇总 */
export function fetchSceneSummary(jobId: number): Promise<SceneSummary> {
  return get<SceneSummary>(`/scene/jobs/${jobId}/scenes`)
}

/** 获取任务切出的片段列表 */
export function fetchSceneClips(jobId: number): Promise<SceneClip[]> {
  return get<SceneClip[]>(`/scene/jobs/${jobId}/clips`)
}

/** 取消任务 */
export function cancelSceneJob(jobId: number): Promise<SceneJob> {
  return post<SceneJob>(`/scene/jobs/${jobId}/cancel`)
}

/** 删除任务记录（磁盘上已切出的片段文件保留） */
export function deleteSceneJob(jobId: number): Promise<{ id: number }> {
  return del<{ id: number }>(`/scene/jobs/${jobId}`)
}
