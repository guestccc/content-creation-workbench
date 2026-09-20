/**
 * 智能混剪相关的接口调用。
 *
 * 每个函数对应后端一个接口，返回值已完成拆包（直接拿到 data）。
 */

import { del, get, post, put } from './client'
import type {
  MixEnvironment,
  MixJob,
  MixJobListData,
  MixJobPayload,
  MixLibraryData,
  MixSource,
} from '../types/mix'

/** 探测 ffmpeg / ffprobe 是否可用，并拿回默认素材/输出目录 */
export function fetchMixEnvironment(): Promise<MixEnvironment> {
  return get<MixEnvironment>('/mix/environment')
}

/** 已添加的素材目录清单 */
export function fetchMixSources(): Promise<MixSource[]> {
  return get<MixSource[]>('/mix/sources')
}

/** 把一个本地目录添加为素材目录（不复制、不移动文件） */
export function addMixSource(path: string): Promise<MixSource> {
  return post<MixSource>('/mix/sources', { path })
}

/** 移除素材目录（磁盘上的文件不受影响） */
export function removeMixSource(sourceId: string): Promise<{ id: string }> {
  return del<{ id: string }>(`/mix/sources/${sourceId}`)
}

/** 扫描所有素材目录（目录 + 素材 + 时长） */
export function fetchMixLibrary(): Promise<MixLibraryData> {
  return get<MixLibraryData>('/mix/library')
}

/** 创建混剪任务 */
export function createMixJob(payload: MixJobPayload): Promise<MixJob> {
  return post<MixJob>('/mix/jobs', payload)
}

/** 分页查询历史任务（不返回每条成片的明细） */
export function fetchMixJobs(params: {
  page?: number
  page_size?: number
  status?: string
}): Promise<MixJobListData> {
  return get<MixJobListData>('/mix/jobs', { ...params })
}

/** 获取任务详情（轮询进度也用它） */
export function fetchMixJob(jobId: number): Promise<MixJob> {
  return get<MixJob>(`/mix/jobs/${jobId}`)
}

/** 取消任务 */
export function cancelMixJob(jobId: number): Promise<MixJob> {
  return post<MixJob>(`/mix/jobs/${jobId}/cancel`)
}

/** 更新任务备注（空串表示清空，最多 200 字）；返回更新后的任务 */
export function updateMixJobRemark(jobId: number, remark: string): Promise<MixJob> {
  return put<MixJob>(`/mix/jobs/${jobId}/remark`, { remark })
}

/** 删除任务记录；purgeFiles=true 时连同磁盘上的成片与输出目录一起删除 */
export function deleteMixJob(jobId: number, purgeFiles = false): Promise<{ id: number }> {
  return del<{ id: number }>(`/mix/jobs/${jobId}`, { purge_files: purgeFiles })
}

/** 批量删除任务记录（整批成功或整批失败）；purgeFiles=true 时产物一并清掉 */
export function batchDeleteMixJobs(
  ids: number[],
  purgeFiles = false,
): Promise<{ ids: number[]; count: number }> {
  return post<{ ids: number[]; count: number }>('/mix/jobs/batch-delete', {
    ids,
    purge_files: purgeFiles,
  })
}
