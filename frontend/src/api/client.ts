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

/**
 * 把任意异常转成给用户看的文案。
 *
 * ApiError（后端的业务错误、网络超时等）用后端给的 message —— 它比任何
 * 兜底文案都具体；其余异常（前端自己抛的、第三方库抛的）才用兜底文案。
 * 页面里所有的 catch 分支都走这里，不各写各的三元表达式。
 */
export function describeError(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback
}

/** 接口基础路径：开发环境走 vite 代理，生产环境可通过环境变量覆盖。
 *  导出给拼静态资源地址的场景（如素材抓取的本地媒体 img/video src）。 */
export const BASE_URL: string = import.meta.env.VITE_API_BASE_URL || '/api/v1'

/** 请求超时时间（毫秒），可在单次请求上用 timeoutMs 覆盖 */
const TIMEOUT_MS = 15000

/** 请求配置：在 fetch 的基础上支持按请求覆盖超时（LLM 这类长请求要用）。 */
export interface RequestOptions extends RequestInit {
  /** 本次请求的超时毫秒数；不传用默认 15 秒 */
  timeoutMs?: number
}

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
 * @param options fetch 配置；timeoutMs 可覆盖默认 15 秒超时（LLM 调用这类
 *   同步长请求必须覆盖，否则必然超时）
 * @returns 响应体中的 data 字段
 * @throws ApiError 网络异常、超时或后端返回错误时抛出
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  // 超时控制：避免后端无响应时页面一直处于加载状态
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), options.timeoutMs ?? TIMEOUT_MS)

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
export function get<T>(
  path: string,
  params?: Record<string, unknown>,
  timeoutMs?: number,
): Promise<T> {
  return request<T>(`${path}${buildQuery(params)}`, { timeoutMs })
}

/** POST 请求 */
export function post<T>(path: string, body?: unknown, timeoutMs?: number): Promise<T> {
  return request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}), timeoutMs })
}

/** PUT 请求 */
export function put<T>(path: string, body?: unknown, timeoutMs?: number): Promise<T> {
  return request<T>(path, { method: 'PUT', body: JSON.stringify(body ?? {}), timeoutMs })
}

/** DELETE 请求 */
export function del<T>(
  path: string,
  params?: Record<string, unknown>,
  timeoutMs?: number,
): Promise<T> {
  return request<T>(`${path}${buildQuery(params)}`, { method: 'DELETE', timeoutMs })
}

/** 一帧 SSE 的处理器：event 是帧名，data 是帧体解析出来的对象 */
export type StreamHandler = (event: string, data: Record<string, unknown>) => void

/**
 * 发起一个 SSE 请求，逐帧回调（AI 文案生成这类要展示思考过程的接口用它）。
 *
 * 为什么不复用 request()：那边写死了 `Content-Type: application/json` 并且
 * 强制 `response.json()` 整包拆开，流式响应的 body 根本没法那么读。
 *
 * **不在这里设超时**：生成一轮可能要一两分钟，前端拿秒表掐断没有任何好处；
 * 真正的超时由后端（AI_TIMEOUT_SECONDS）兜底，取消由调用方传 AbortSignal。
 *
 * @param path 接口路径，如 /crawl/jobs/1/ai-copies/stream
 * @param body 请求体（会被 JSON 序列化）
 * @param onEvent 每收到一帧调用一次
 * @param signal 调用方的取消信号（关弹窗、重新生成时用来掐断上一轮）
 * @throws ApiError 连不上、或预检失败（404/422 这类不返回流的情况）
 */
export async function postStream(
  path: string,
  body: unknown,
  onEvent: StreamHandler,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      method: 'POST',
      body: JSON.stringify(body ?? {}),
      headers: { 'Content-Type': 'application/json' },
      signal,
    })
  } catch (error) {
    // 自己取消的照原样抛出去：调用方要能分清「用户关掉了」和「真的连不上」
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw error
    }
    throw new ApiError('无法连接后端服务，请确认服务已启动', 'NETWORK_ERROR', 0)
  }

  if (!response.ok) {
    // 预检失败（任务不存在 / 笔记不在结果里）不是流，是普通 JSON 错误体：
    // 这条路上要能给出后端的原话，别用一句「HTTP 404」把人挡回去
    let payload: ApiEnvelope<unknown> | null = null
    try {
      payload = (await response.json()) as ApiEnvelope<unknown>
    } catch {
      payload = null
    }
    throw new ApiError(
      payload?.error?.message ?? `请求失败（HTTP ${response.status}）`,
      payload?.error?.code ?? `HTTP_${response.status}`,
      response.status,
      payload?.error?.details,
    )
  }

  const reader = response.body?.getReader()
  if (!reader) {
    throw new ApiError('响应不是可读的流', 'INVALID_RESPONSE', response.status)
  }

  const decoder = new TextDecoder()
  // 网络分块与 SSE 帧不是一一对应的：一帧可能横跨两块，也可能一块里好几帧，
  // 所以必须自己攒缓冲、按空行切
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) {
        break
      }
      buffer += decoder.decode(value, { stream: true })
      let cut = buffer.indexOf('\n\n')
      while (cut !== -1) {
        emitFrame(buffer.slice(0, cut), onEvent)
        buffer = buffer.slice(cut + 2)
        cut = buffer.indexOf('\n\n')
      }
    }
  } finally {
    reader.releaseLock()
  }
}

/** 拆一帧 `event: X\ndata: {...}` 并回调；缺字段或坏 JSON 的帧跳过。 */
function emitFrame(frame: string, onEvent: StreamHandler): void {
  let event = ''
  let data = ''
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) {
      event = line.slice('event:'.length).trim()
    } else if (line.startsWith('data:')) {
      data = line.slice('data:'.length).trim()
    }
  }
  // 注释行（`: keep-alive`）与空行没有 event，直接跳过
  if (!event || !data) {
    return
  }
  try {
    onEvent(event, JSON.parse(data) as Record<string, unknown>)
  } catch {
    // 单帧坏掉不毁整轮：跳过它，后面的帧照常处理
    console.warn('SSE 帧解析失败，已跳过', frame)
  }
}
