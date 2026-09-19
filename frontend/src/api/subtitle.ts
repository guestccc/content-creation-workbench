/**
 * 视频字幕提取相关的接口调用。
 *
 * 每个函数对应后端一个接口，返回值已完成拆包（直接拿到 data）。
 */

import { del, get, post, put } from './client'
import type {
  SubtitleEnvironment,
  SubtitleJob,
  SubtitleJobListData,
  SubtitleJobPayload,
  SubtitleFile,
  SubtitleText,
} from '../types/subtitle'

/** 探测 VideoCaptioner / ffmpeg 是否可用，未安装时返回按平台的安装指引 */
export function fetchSubtitleEnvironment(refresh = false): Promise<SubtitleEnvironment> {
  return get<SubtitleEnvironment>('/subtitle/environment', refresh ? { refresh: true } : undefined)
}

/**
 * 手动指定 VideoCaptioner 的安装目录（path 为空串表示恢复自动探测）。
 *
 * 后端会把路径写进 backend/.env 并原地热更新，返回值就是最新的自检结果，
 * 调用方直接拿它渲染，不必再发一次 GET。
 */
export function updateSubtitleVcRoot(path: string): Promise<SubtitleEnvironment> {
  return put<SubtitleEnvironment>('/subtitle/environment/vc-root', { path })
}

/** 创建字幕提取任务 */
export function createSubtitleJob(payload: SubtitleJobPayload): Promise<SubtitleJob> {
  return post<SubtitleJob>('/subtitle/jobs', payload)
}

/** 分页查询历史任务（不返回每条视频的明细） */
export function fetchSubtitleJobs(params: {
  page?: number
  page_size?: number
  status?: string
}): Promise<SubtitleJobListData> {
  return get<SubtitleJobListData>('/subtitle/jobs', { ...params })
}

/** 获取任务详情（轮询进度也用它） */
export function fetchSubtitleJob(jobId: number): Promise<SubtitleJob> {
  return get<SubtitleJob>(`/subtitle/jobs/${jobId}`)
}

/** 获取任务已产出的字幕文件列表 */
export function fetchSubtitleFiles(jobId: number): Promise<SubtitleFile[]> {
  return get<SubtitleFile[]>(`/subtitle/jobs/${jobId}/subtitles`)
}

/** 获取单份字幕的文本内容（预览用，超长会被截断） */
export function fetchSubtitleText(jobId: number, index: number): Promise<SubtitleText> {
  return get<SubtitleText>(`/subtitle/jobs/${jobId}/subtitles/${index}`)
}

/** 取消任务 */
export function cancelSubtitleJob(jobId: number): Promise<SubtitleJob> {
  return post<SubtitleJob>(`/subtitle/jobs/${jobId}/cancel`)
}

/** 删除任务记录；purgeFiles=true 时连同磁盘上的字幕文件一起删除 */
export function deleteSubtitleJob(jobId: number, purgeFiles = false): Promise<{ id: number }> {
  return del<{ id: number }>(`/subtitle/jobs/${jobId}`, { purge_files: purgeFiles })
}

/** 批量删除任务记录（整批成功或整批失败）；purgeFiles=true 时字幕文件一并清掉 */
export function batchDeleteSubtitleJobs(
  ids: number[],
  purgeFiles = false,
): Promise<{ ids: number[]; count: number }> {
  return post<{ ids: number[]; count: number }>('/subtitle/jobs/batch-delete', {
    ids,
    purge_files: purgeFiles,
  })
}
