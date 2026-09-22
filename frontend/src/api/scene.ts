/**
 * 智能镜头分割相关的接口调用。
 *
 * 每个函数对应后端一个接口，返回值已完成拆包（直接拿到 data）。
 */

import { del, get, post, put } from './client'
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

/** 更新任务备注（空串表示清空，最多 200 字）；返回更新后的任务 */
export function updateSceneJobRemark(jobId: number, remark: string): Promise<SceneJob> {
  return put<SceneJob>(`/scene/jobs/${jobId}/remark`, { remark })
}

/** 重试单条失败 / 跳过的视频：条目重置回 pending，任务重新入队（其余条目结果不动） */
export function retrySceneItem(jobId: number, itemIndex: number): Promise<SceneJob> {
  return post<SceneJob>(`/scene/jobs/${jobId}/items/${itemIndex}/retry`)
}

/** 重试全部失败 / 跳过的视频：一条请求搞定，返回重置后的整条任务 */
export function retrySceneJob(jobId: number): Promise<SceneJob> {
  return post<SceneJob>(`/scene/jobs/${jobId}/retry`)
}

/** 删除任务记录；purgeFiles=true 时连同磁盘上的切片产物一起删除 */
export function deleteSceneJob(jobId: number, purgeFiles = false): Promise<{ id: number }> {
  return del<{ id: number }>(`/scene/jobs/${jobId}`, { purge_files: purgeFiles })
}

/** 批量删除任务记录（整批成功或整批失败）；purgeFiles=true 时产物一并清掉 */
export function batchDeleteSceneJobs(
  ids: number[],
  purgeFiles = false,
): Promise<{ ids: number[]; count: number }> {
  return post<{ ids: number[]; count: number }>('/scene/jobs/batch-delete', {
    ids,
    purge_files: purgeFiles,
  })
}
