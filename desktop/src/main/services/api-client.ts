/**
 * 后端接口客户端（运行在主进程）。
 *
 * 渲染进程不直接访问网络，所有请求都经由主进程发出，
 * 这样可以统一处理超时、错误码，也让后端地址成为单一配置项。
 */

import type {
  Account,
  AccountListData,
  AccountPayload,
  Content,
  ContentQuery,
  ListData,
  PublishTask,
  PublishTaskStatistics,
  TaskQuery
} from '@shared/types'

import { appConfig } from './app-config'

/** 单次请求超时时间（毫秒） */
const REQUEST_TIMEOUT_MS = 15_000

/** 后端返回的错误结构 */
interface BackendError {
  code: string
  message: string
  details?: unknown
}

/** 调用后端失败时抛出的统一异常 */
export class ApiError extends Error {
  readonly code: string
  readonly status: number

  constructor(message: string, code = 'API_ERROR', status = 0) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

/** 后端统一响应包装 */
interface Envelope<T> {
  success: boolean
  data?: T
  error?: BackendError
}

/**
 * 发起一次后端请求并解开响应包装。
 *
 * @param path 以 / 开头的接口路径
 * @param init fetch 参数
 * @throws ApiError 网络异常、超时、后端返回错误
 */
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const baseUrl = appConfig.read().backendBaseUrl
  const url = `${baseUrl}/api/v1${path}`

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)

  let response: Response
  try {
    response = await fetch(url, {
      ...init,
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        ...(init.headers ?? {})
      }
    })
  } catch (error) {
    const aborted = error instanceof Error && error.name === 'AbortError'
    throw new ApiError(
      aborted
        ? `请求超时（${REQUEST_TIMEOUT_MS / 1000} 秒），请确认后端服务是否正常`
        : `无法连接后端服务（${baseUrl}），请检查服务是否已启动`,
      aborted ? 'TIMEOUT' : 'NETWORK_ERROR'
    )
  } finally {
    clearTimeout(timer)
  }

  let payload: Envelope<T>
  try {
    payload = (await response.json()) as Envelope<T>
  } catch {
    // 后端返回了非 JSON 内容（例如反向代理的错误页）
    throw new ApiError(
      `后端返回了无法解析的内容（HTTP ${response.status}）`,
      'INVALID_RESPONSE',
      response.status
    )
  }

  if (!response.ok || payload.success === false) {
    const error = payload.error
    throw new ApiError(
      error?.message || `请求失败（HTTP ${response.status}）`,
      error?.code || 'HTTP_ERROR',
      response.status
    )
  }

  return payload.data as T
}

/** 拼接查询字符串，跳过空值 */
function buildQuery(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') {
      continue
    }
    search.append(key, String(value))
  }
  const query = search.toString()
  return query ? `?${query}` : ''
}

/** 后端接口封装 */
export const api = {
  /** 健康检查，同时返回后端版本与数据库状态 */
  async ping(): Promise<{ version: string; database: string }> {
    return request<{ version: string; database: string }>('/health')
  },

  accounts: {
    async list(): Promise<AccountListData> {
      return request<AccountListData>('/accounts')
    },

    async create(payload: AccountPayload): Promise<Account> {
      return request<Account>('/accounts', {
        method: 'POST',
        body: JSON.stringify(payload)
      })
    },

    async update(id: number, payload: Partial<AccountPayload>): Promise<Account> {
      return request<Account>(`/accounts/${id}`, {
        method: 'PUT',
        body: JSON.stringify(payload)
      })
    },

    async remove(id: number): Promise<{ id: number }> {
      return request<{ id: number }>(`/accounts/${id}`, { method: 'DELETE' })
    }
  },

  contents: {
    async list(query: ContentQuery = {}): Promise<ListData<Content>> {
      const qs = buildQuery({
        page: query.page,
        page_size: query.page_size,
        status: query.status,
        keyword: query.keyword
      })
      return request<ListData<Content>>(`/contents${qs}`)
    },

    async get(id: number): Promise<Content> {
      return request<Content>(`/contents/${id}`)
    }
  },

  tasks: {
    async list(query: TaskQuery = {}): Promise<ListData<PublishTask>> {
      const qs = buildQuery({
        page: query.page,
        page_size: query.page_size,
        status: query.status,
        account_id: query.account_id,
        content_id: query.content_id
      })
      return request<ListData<PublishTask>>(`/publish-tasks${qs}`)
    },

    async batchCreate(payload: {
      content_id: number
      account_ids: number[]
      scheduled_at?: string | null
      max_retries?: number
    }): Promise<PublishTask[]> {
      return request<PublishTask[]>('/publish-tasks/batch', {
        method: 'POST',
        body: JSON.stringify(payload)
      })
    },

    /** 认领待执行任务，被认领的任务在后端立即转为发布中 */
    async claim(limit: number, accountId?: number): Promise<PublishTask[]> {
      return request<PublishTask[]>('/publish-tasks/claim', {
        method: 'POST',
        body: JSON.stringify({ limit, account_id: accountId ?? null })
      })
    },

    /** 上报发布结果 */
    async report(
      taskId: number,
      payload: { success: boolean; result_url?: string; error_message?: string }
    ): Promise<PublishTask> {
      return request<PublishTask>(`/publish-tasks/${taskId}/report`, {
        method: 'POST',
        body: JSON.stringify(payload)
      })
    },

    async cancel(id: number): Promise<PublishTask> {
      return request<PublishTask>(`/publish-tasks/${id}/cancel`, { method: 'POST' })
    },

    async retry(id: number): Promise<PublishTask> {
      return request<PublishTask>(`/publish-tasks/${id}/retry`, { method: 'POST' })
    },

    async remove(id: number): Promise<{ id: number }> {
      return request<{ id: number }>(`/publish-tasks/${id}`, { method: 'DELETE' })
    },

    async statistics(): Promise<PublishTaskStatistics> {
      return request<PublishTaskStatistics>('/publish-tasks/statistics')
    }
  }
}
