/**
 * 素材抓取相关的接口调用。
 *
 * 每个函数对应后端一个接口，返回值已完成拆包（直接拿到 data）。
 * crawlMediaUrl 不是请求函数：本地媒体地址直接给 <img>/<video> 的 src 用。
 */

import { BASE_URL, del, get, post } from './client'
import type {
  CrawlEnvironment,
  CrawlJob,
  CrawlJobListData,
  CrawlJobPayload,
  CrawlLogData,
  CrawlResultsData,
} from '../types/crawler'

/** 探测 MediaCrawler / Node / 登录态缓存，未就绪时返回分步安装指引 */
export function fetchCrawlEnvironment(refresh = false): Promise<CrawlEnvironment> {
  return get<CrawlEnvironment>('/crawl/environment', refresh ? { refresh: true } : undefined)
}

/** 创建抓取任务（后台队列执行，进度靠轮询任务详情） */
export function createCrawlJob(payload: CrawlJobPayload): Promise<CrawlJob> {
  return post<CrawlJob>('/crawl/jobs', payload)
}

/** 分页查询历史任务（可按状态 / 平台过滤） */
export function fetchCrawlJobs(params: {
  page?: number
  page_size?: number
  status?: string
  platform?: string
}): Promise<CrawlJobListData> {
  return get<CrawlJobListData>('/crawl/jobs', { ...params })
}

/** 获取任务详情（轮询进度也用它） */
export function fetchCrawlJob(jobId: number): Promise<CrawlJob> {
  return get<CrawlJob>(`/crawl/jobs/${jobId}`)
}

/** 获取任务的归一化笔记列表（跨平台字段已对齐） */
export function fetchCrawlResults(jobId: number): Promise<CrawlResultsData> {
  return get<CrawlResultsData>(`/crawl/jobs/${jobId}/results`)
}

/** 获取 MC 子进程日志尾部（排查失败用） */
export function fetchCrawlLog(jobId: number, limit?: number): Promise<CrawlLogData> {
  return get<CrawlLogData>(`/crawl/jobs/${jobId}/log`, limit ? { limit } : undefined)
}

/** 取消任务（执行中的会整组结束 MC 进程，已抓内容保留） */
export function cancelCrawlJob(jobId: number): Promise<CrawlJob> {
  return post<CrawlJob>(`/crawl/jobs/${jobId}/cancel`)
}

/** 删除任务记录；purgeFiles=true 时连同磁盘上的 jsonl 与媒体文件一起删除 */
export function deleteCrawlJob(jobId: number, purgeFiles = false): Promise<{ id: number }> {
  return del<{ id: number }>(`/crawl/jobs/${jobId}`, { purge_files: purgeFiles })
}

/** 批量删除任务记录（整批成功或整批失败）；purgeFiles=true 时产物一并清掉 */
export function batchDeleteCrawlJobs(
  ids: number[],
  purgeFiles = false,
): Promise<{ ids: number[]; count: number }> {
  return post<{ ids: number[]; count: number }>('/crawl/jobs/batch-delete', {
    ids,
    purge_files: purgeFiles,
  })
}

/**
 * 本地媒体文件的访问地址（relative 为相对任务输出目录的路径）。
 *
 * 不是请求函数：给 <img src> / <video src> 直接用。路径逐段 encode，
 * Windows 下的中文文件名（如博主昵称目录）才不会 404。
 */
export function crawlMediaUrl(jobId: number, relative: string): string {
  const encoded = relative.split('/').map(encodeURIComponent).join('/')
  return `${BASE_URL}/crawl/jobs/${jobId}/media/${encoded}`
}
