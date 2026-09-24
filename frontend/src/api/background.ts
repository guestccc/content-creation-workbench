/**
 * 一键换背景相关的接口调用。
 *
 * 每个函数对应后端一个接口，返回值已完成拆包（直接拿到 data）。
 *
 * 产物图走的是任务级端点（任务 ID + 序号），不是 /fs/preview：产物路径只在任务
 * 记录里，前端传不了也猜不出。地址用 BASE_URL 拼，直接塞给 <img src>。
 */

import { BASE_URL, del, get, post, put } from './client'
import type {
  BackgroundJob,
  BackgroundJobListData,
  BackgroundJobPayload,
} from '../types/background'

/** 创建换背景任务 */
export function createBackgroundJob(payload: BackgroundJobPayload): Promise<BackgroundJob> {
  return post<BackgroundJob>('/background/jobs', payload)
}

/**
 * 分页查询历史任务（不返回每张图的明细）。
 *
 * 传 `source_crawl_job_id` 只列出来自该素材抓取任务的那些 —— 素材抓取页拿它
 * 一次性把「这条抓取任务派生出的换背景任务」全捞出来，按笔记 id 分组挂到笔记行上。
 */
export function fetchBackgroundJobs(params: {
  page?: number
  page_size?: number
  status?: string
  source_crawl_job_id?: number
}): Promise<BackgroundJobListData> {
  return get<BackgroundJobListData>('/background/jobs', { ...params })
}

/** 获取任务详情（轮询进度也用它，含每张图的结果与诊断统计） */
export function fetchBackgroundJob(jobId: number): Promise<BackgroundJob> {
  return get<BackgroundJob>(`/background/jobs/${jobId}`)
}

/** 取消任务 */
export function cancelBackgroundJob(jobId: number): Promise<BackgroundJob> {
  return post<BackgroundJob>(`/background/jobs/${jobId}/cancel`)
}

/** 重试单条失败 / 跳过的图片：条目重置回 pending，任务重新入队（其余条目结果不动） */
export function retryBackgroundItem(jobId: number, index: number): Promise<BackgroundJob> {
  return post<BackgroundJob>(`/background/jobs/${jobId}/items/${index}/retry`)
}

/** 重试全部失败 / 跳过的图片：一条请求搞定，返回重置后的整条任务 */
export function retryBackgroundJob(jobId: number): Promise<BackgroundJob> {
  return post<BackgroundJob>(`/background/jobs/${jobId}/retry`)
}

/** 更新任务备注（空串表示清空，最多 200 字）；返回更新后的任务 */
export function updateBackgroundJobRemark(jobId: number, remark: string): Promise<BackgroundJob> {
  return put<BackgroundJob>(`/background/jobs/${jobId}/remark`, { remark })
}

/** 删除任务记录；purgeFiles=true 时连同磁盘上的产物目录一起删除 */
export function deleteBackgroundJob(jobId: number, purgeFiles = false): Promise<{ id: number }> {
  return del<{ id: number }>(`/background/jobs/${jobId}`, { purge_files: purgeFiles })
}

/** 批量删除任务记录（整批成功或整批失败）；purgeFiles=true 时产物一并清掉 */
export function batchDeleteBackgroundJobs(
  ids: number[],
  purgeFiles = false,
): Promise<{ ids: number[]; count: number }> {
  return post<{ ids: number[]; count: number }>('/background/jobs/batch-delete', {
    ids,
    purge_files: purgeFiles,
  })
}

/**
 * 某张产物的图片地址（直接塞给 <img src> / antd Image）。
 *
 * 序号从 1 开始，越界与「这张还没产出」都会 404 —— 前端只关心能不能显示，
 * 不必先查一次状态再决定渲不渲染。
 */
export function backgroundOutputUrl(jobId: number, index: number): string {
  return `${BASE_URL}/background/jobs/${jobId}/items/${index}/output`
}
