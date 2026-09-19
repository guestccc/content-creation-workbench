/**
 * 一键成品（finalcut）相关的接口调用。
 *
 * 每个函数对应后端一个接口，返回值已完成拆包（直接拿到 data）。
 * 两个任务域：copy-jobs（AI 文案）与 render-jobs（烧字合成）。
 */

import { del, get, post, put, BASE_URL } from './client'
import type {
  AiSettings,
  AiSettingsPayload,
  CopyJobPayload,
  FinalcutCopyJob,
  FinalcutCopyJobListData,
  FinalcutEnvironment,
  FinalcutRenderJob,
  FinalcutRenderJobListData,
  FinalcutSources,
  RenderJobPayload,
} from '../types/finalcut'

// ---------------------------------------------------------------------------
// 环境自检与 AI 配置
// ---------------------------------------------------------------------------

/** 探测 AI 配置 / ffmpeg-drawtext / 中文字体；refresh=true 顺带从 .env 热同步 */
export function fetchFinalcutEnvironment(refresh = false): Promise<FinalcutEnvironment> {
  return get<FinalcutEnvironment>('/finalcut/environment', { refresh: refresh || undefined })
}

/** 读 AI 配置（key 只有掩码，完整值不出后端） */
export function fetchAiSettings(): Promise<AiSettings> {
  return get<AiSettings>('/finalcut/settings')
}

/** 保存 AI 配置（写回 backend/.env 并热同步；api_key 留空 = 不改） */
export function updateAiSettings(payload: AiSettingsPayload): Promise<AiSettings> {
  return put<AiSettings>('/finalcut/settings', payload)
}

// ---------------------------------------------------------------------------
// 素材来源与本地视频预览
// ---------------------------------------------------------------------------

/** 历史产物来源清单：混剪的成片 + 字幕任务的 .srt */
export function fetchFinalcutSources(): Promise<FinalcutSources> {
  return get<FinalcutSources>('/finalcut/sources')
}

/** 任意本地视频的只读预览流地址（给 <video src> 用，支持 Range） */
export function finalcutPreviewUrl(path: string): string {
  return `${BASE_URL}/finalcut/preview?path=${encodeURIComponent(path)}`
}

// ---------------------------------------------------------------------------
// 文案任务
// ---------------------------------------------------------------------------

/** 创建文案生成任务 */
export function createCopyJob(payload: CopyJobPayload): Promise<FinalcutCopyJob> {
  return post<FinalcutCopyJob>('/finalcut/copy-jobs', payload)
}

/** 分页查询文案任务 */
export function fetchCopyJobs(params: {
  page?: number
  page_size?: number
  status?: string
}): Promise<FinalcutCopyJobListData> {
  return get<FinalcutCopyJobListData>('/finalcut/copy-jobs', { ...params })
}

/** 文案任务详情（轮询进度也用它） */
export function fetchCopyJob(jobId: number): Promise<FinalcutCopyJob> {
  return get<FinalcutCopyJob>(`/finalcut/copy-jobs/${jobId}`)
}

/** 取消文案任务（在途的 AI 请求会跑完再丢弃，结果不落库） */
export function cancelCopyJob(jobId: number): Promise<FinalcutCopyJob> {
  return post<FinalcutCopyJob>(`/finalcut/copy-jobs/${jobId}/cancel`)
}

/** 删除文案任务记录（磁盘上无产物，删的就是记录本身） */
export function deleteCopyJob(jobId: number): Promise<{ id: number }> {
  return del<{ id: number }>(`/finalcut/copy-jobs/${jobId}`)
}

/** 批量删除文案任务记录（整批成功或整批失败） */
export function batchDeleteCopyJobs(ids: number[]): Promise<{ ids: number[]; count: number }> {
  return post<{ ids: number[]; count: number }>('/finalcut/copy-jobs/batch-delete', { ids })
}

// ---------------------------------------------------------------------------
// 合成任务
// ---------------------------------------------------------------------------

/** 创建合成任务（勾选的文案 × 框选 × 样式，每条一个成片） */
export function createRenderJob(payload: RenderJobPayload): Promise<FinalcutRenderJob> {
  return post<FinalcutRenderJob>('/finalcut/render-jobs', payload)
}

/** 分页查询合成任务（不返回每条成片的明细） */
export function fetchRenderJobs(params: {
  page?: number
  page_size?: number
  status?: string
}): Promise<FinalcutRenderJobListData> {
  return get<FinalcutRenderJobListData>('/finalcut/render-jobs', { ...params })
}

/** 合成任务详情（含每条成片；轮询进度也用它） */
export function fetchRenderJob(jobId: number): Promise<FinalcutRenderJob> {
  return get<FinalcutRenderJob>(`/finalcut/render-jobs/${jobId}`)
}

/** 取消合成任务（执行中的会整组杀掉当前 ffmpeg） */
export function cancelRenderJob(jobId: number): Promise<FinalcutRenderJob> {
  return post<FinalcutRenderJob>(`/finalcut/render-jobs/${jobId}/cancel`)
}

/** 删除合成任务记录；purgeFiles=true 时连同输出目录一起删 */
export function deleteRenderJob(jobId: number, purgeFiles = false): Promise<{ id: number }> {
  return del<{ id: number }>(`/finalcut/render-jobs/${jobId}`, { purge_files: purgeFiles })
}

/** 批量删除合成任务（整批成功或整批失败）；purgeFiles=true 时产物一并清掉 */
export function batchDeleteRenderJobs(
  ids: number[],
  purgeFiles = false,
): Promise<{ ids: number[]; count: number }> {
  return post<{ ids: number[]; count: number }>('/finalcut/render-jobs/batch-delete', {
    ids,
    purge_files: purgeFiles,
  })
}
