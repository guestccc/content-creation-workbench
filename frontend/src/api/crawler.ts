/**
 * 素材抓取相关的接口调用。
 *
 * 每个函数对应后端一个接口，返回值已完成拆包（直接拿到 data）。
 * crawlMediaUrl 不是请求函数：本地媒体地址直接给 <img>/<video> 的 src 用。
 */

import { BASE_URL, del, get, post, postStream, put } from './client'
import type { StreamHandler } from './client'
import type {
  CrawlCommentRefetchPayload,
  CrawlEnvironment,
  CrawlJob,
  CrawlJobListData,
  CrawlJobPayload,
  CrawlLogData,
  CrawlNoteAiCopyData,
  CrawlResultsData,
  NoteCommentsData,
} from '../types/crawler'

/* 生成走 SSE 流式接口，超时由后端兜底、取消由调用方传 AbortSignal ——
   前端不设秒表（见 client.ts 的 postStream）。 */

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

/**
 * 读取一条笔记已生成的 AI 文案。
 *
 * 没生成过时返回 `{found: false, result: null}` —— 这是正常态不是错误，
 * 调用方据此决定要不要当场生成。
 *
 * note_id 走 query 而不是路径段：平台原生 id 的字符集没保证，含 `/` 会吞路径。
 */
export function fetchNoteAiCopy(jobId: number, noteId: string): Promise<CrawlNoteAiCopyData> {
  return get<CrawlNoteAiCopyData>(`/crawl/jobs/${jobId}/ai-copies`, { note_id: noteId })
}

/**
 * 流式生成（或「换一批」）一条笔记的 AI 文案。
 *
 * 覆盖语义：同一（任务, 笔记）永远只有最新一份，重复调用即换一批。
 * 帧契约（与后端一一对应，见 crawl_jobs.py 的 stream_note_ai_copy）：
 *
 * - `reasoning` —— `{text}`：思维链增量，边到边推给 `<Think>`；
 * - `done`      —— `{result}`：落库完成，result 就是 CrawlNoteAiCopy；
 * - `error`     —— `{code, message}`：失败。AI_NOT_CONFIGURED / AI_AUTH_FAILED
 *   提示去配置，其余提示重试。
 *
 * @param onEvent 每收到一帧回调一次，由调用方（useAiCopy）累积状态
 * @param signal 取消信号：关弹窗、重新生成时要掐断上一轮
 */
export function streamNoteAiCopy(
  jobId: number,
  noteId: string,
  onEvent: StreamHandler,
  signal?: AbortSignal,
): Promise<void> {
  return postStream(
    `/crawl/jobs/${jobId}/ai-copies/stream`,
    { note_id: noteId },
    onEvent,
    signal,
  )
}

/**
 * 读一条笔记的评论（评论树 + 原任务评论配置 + 最新一次补抓状态）。
 *
 * 三件事挤在一个接口里是刻意的：补抓在跑时前端要靠轮询这个接口同时更新
 * 「评论列表」和「补抓进度」，分成两个接口就会出现两边不同步的中间态。
 *
 * note_id 走 query 而不是路径段，与 fetchNoteAiCopy 同口径：平台原生 id 的
 * 字符集没保证，含 `/` 会吞路径。
 */
export function fetchNoteComments(jobId: number, noteId: string): Promise<NoteCommentsData> {
  return get<NoteCommentsData>(`/crawl/jobs/${jobId}/comments`, { note_id: noteId })
}

/**
 * 对一条笔记补抓评论：后端按原任务的平台 / 登录态**新建一条 detail 派生任务**
 * （只抓这一条、开着评论开关），返回那条任务。
 *
 * 派生任务不进历史任务列表 —— 它的状态只能从 fetchNoteComments 的 refetch 字段看。
 * 同一条笔记已有非终态补抓时后端返 409，details 里带已存在那条的 job_id。
 */
export function refetchNoteComments(
  jobId: number,
  payload: CrawlCommentRefetchPayload,
): Promise<CrawlJob> {
  return post<CrawlJob>(`/crawl/jobs/${jobId}/comments/refetch`, payload)
}

/** 取消任务（执行中的会整组结束 MC 进程，已抓内容保留） */
export function cancelCrawlJob(jobId: number): Promise<CrawlJob> {
  return post<CrawlJob>(`/crawl/jobs/${jobId}/cancel`)
}

/**
 * 重试整条抓取任务：后端按原参数**新建一条任务**并返回它（新 id、新输出目录）。
 *
 * 抓取没有条目级状态（一条任务就是一个 MediaCrawler 子进程），所以只有整任务
 * 重跑这一种形态；而必须是新建 —— 输出目录按任务 id 定死、MC 的 jsonl 又是追加
 * 语义，就地重跑会把条数算成两倍。调用方拿返回的新任务 id 去刷新历史列表。
 *
 * cookie 登录的任务也能重试：cookie 只存在数据库行里、任何响应都不带它，
 * 所以这一步在服务端做（前端重建不出这种任务）。
 */
export function retryCrawlJob(jobId: number): Promise<CrawlJob> {
  return post<CrawlJob>(`/crawl/jobs/${jobId}/retry`)
}

/** 更新任务备注（空串表示清空，最多 200 字）；返回更新后的任务 */
export function updateCrawlJobRemark(jobId: number, remark: string): Promise<CrawlJob> {
  return put<CrawlJob>(`/crawl/jobs/${jobId}/remark`, { remark })
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
