/**
 * HTTP 客户端封装。
 *
 * 统一处理三件事：
 * 1. 请求地址拼接与超时控制；
 * 2. 拆解后端统一响应结构 { success, data } / { success, error }；
 * 3. 把网络异常、超时、业务错误统一转换为 ApiError，页面只需 catch 一种异常。
 */

/** 后端错误响应中的错误体 */
interface ApiErrorBody {
  code: string
  message: string
  details?: unknown
}

/** 后端统一响应结构 */
interface ApiEnvelope<T> {
  success: boolean
  data?: T
  error?: ApiErrorBody
}

/** 统一的接口异常 */
export class ApiError extends Error {
  /** 业务错误码，如 NOT_FOUND、VALIDATION_ERROR */
  readonly code: string
  /** HTTP 状态码，网络层失败时为 0 */
  readonly status: number
  /** 字段级校验明细（校验失败时存在） */
  readonly details?: unknown

  constructor(message: string, code: string, status: number, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.details = details
  }
}

/** 接口基础路径：开发环境走 vite 代理，生产环境可通过环境变量覆盖 */
const BASE_URL: string = import.meta.env.VITE_API_BASE_URL || '/api/v1'

/** 请求超时时间（毫秒） */
const TIMEOUT_MS = 15000

/** 把查询参数拼接为 query string，自动跳过空值 */
function buildQuery(params?: Record<string, unknown>): string {
  if (!params) {
    return ''
  }

  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    // 空字符串 / null / undefined 一律不传，避免后端把它当成有效过滤条件
    if (value === undefined || value === null || value === '') {
      continue
    }
    search.append(key, String(value))
  }

  const query = search.toString()
  return query ? `?${query}` : ''
}

/**
 * 发起请求并拆解统一响应结构。
 *
 * @param path 接口路径，如 /contents
 * @param options fetch 配置
 * @returns 响应体中的 data 字段
 * @throws ApiError 网络异常、超时或后端返回错误时抛出
 */
export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  // 超时控制：避免后端无响应时页面一直处于加载状态
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), TIMEOUT_MS)

  let response: Response
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      ...options,
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        ...options.headers,
      },
    })
  } catch (error) {
    // fetch 仅在网络层失败时 reject，HTTP 4xx/5xx 不会走到这里
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError('请求超时，请检查后端服务是否正常运行', 'TIMEOUT', 0)
    }
    throw new ApiError('无法连接后端服务，请确认服务已启动', 'NETWORK_ERROR', 0)
  } finally {
    window.clearTimeout(timer)
  }

  // 解析响应体；后端异常时可能返回非 JSON 内容（例如网关错误页）
  let payload: ApiEnvelope<T> | null = null
  try {
    payload = (await response.json()) as ApiEnvelope<T>
  } catch {
    payload = null
  }

  if (!response.ok || payload?.success === false) {
    const body = payload?.error
    throw new ApiError(
      body?.message ?? `请求失败（HTTP ${response.status}）`,
      body?.code ?? `HTTP_${response.status}`,
      response.status,
      body?.details,
    )
  }

  if (payload?.data === undefined) {
    throw new ApiError('响应数据格式异常', 'INVALID_RESPONSE', response.status)
  }

  return payload.data
}

/** GET 请求 */
export function get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  return request<T>(`${path}${buildQuery(params)}`)
}

/** POST 请求 */
export function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })
}

/** PUT 请求 */
export function put<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: 'PUT', body: JSON.stringify(body ?? {}) })
}

/** DELETE 请求 */
export function del<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'DELETE' })
}
