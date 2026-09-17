/**
 * 主进程 / 预加载 / 渲染进程共用的类型定义。
 *
 * 这里只放纯类型，不放有副作用的运行时代码，
 * 保证被三个构建目标同时引用时不会互相污染。
 */

// ==========================================================================
// 后端接口模型（与 backend/app/schemas 保持一致）
// ==========================================================================

/** 内容状态 */
export type ContentStatus = 'draft' | 'reviewing' | 'published' | 'archived'

/** 平台账号状态 */
export type AccountStatus = 'active' | 'disabled' | 'expired'

/** 发布任务状态 */
export type PublishTaskStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'cancelled'

/** 内容 */
export interface Content {
  id: number
  title: string
  body: string
  platform: string
  status: ContentStatus
  tags: string[]
  author: string
  created_at: string
  updated_at: string
}

/** 分页列表包装 */
export interface ListData<T> {
  total: number
  page: number
  page_size: number
  items: T[]
}

/** 账号（不含任何凭证字段，凭证只存在客户端本地） */
export interface Account {
  id: number
  platform: string
  nickname: string
  account_uid: string
  status: AccountStatus
  remark: string
  created_at: string
  updated_at: string
}

/** 账号列表返回（后端对账号不分页） */
export interface AccountListData {
  total: number
  items: Account[]
}

/** 发布任务 */
export interface PublishTask {
  id: number
  content_id: number
  content_title: string
  account_id: number
  account_nickname: string
  platform: string
  status: PublishTaskStatus
  scheduled_at: string | null
  started_at: string | null
  finished_at: string | null
  retry_count: number
  max_retries: number
  error_message: string
  result_url: string
  created_at: string
  updated_at: string
}

/** 发布任务统计 */
export interface PublishTaskStatistics {
  total: number
  by_status: Record<PublishTaskStatus, number>
}

/** 内容统计 */
export interface ContentStatistics {
  total: number
  by_status: Record<ContentStatus, number>
}

// ==========================================================================
// 本地凭证（仅存在于客户端，永不发送到后端）
// ==========================================================================

/** 凭证类型 */
export type CredentialType = 'cookie' | 'token' | 'password'

/** 一条凭证的明文结构，落盘前会被 safeStorage 加密 */
export interface CredentialPayload {
  type: CredentialType
  /** 凭证主体：cookie 串 / token 串 / 密码 */
  secret: string
  /** 账号名，password 类型时使用 */
  username?: string
  /** 过期时间（ISO8601），可留空 */
  expiresAt?: string | null
  /** 附加字段：设备指纹、代理备注等 */
  extra?: Record<string, string>
}

/** 凭证在界面上的展示形态，secret 已脱敏 */
export interface CredentialSummary {
  accountId: number
  type: CredentialType
  /** 脱敏后的凭证预览，例如 "abcd****wxyz" */
  maskedSecret: string
  username?: string
  expiresAt?: string | null
  updatedAt: string
}

/** 凭证存储的可用性状态 */
export interface CredentialStoreStatus {
  /** 系统加密是否可用（macOS 上由钥匙串提供） */
  available: boolean
  /** 不可用时的原因说明 */
  message: string
  /** 已保存凭证的账号 ID 列表 */
  savedAccountIds: number[]
}

// ==========================================================================
// 本地应用配置
// ==========================================================================

export interface AppConfig {
  /** 后端服务地址 */
  backendBaseUrl: string
  /** 轮询间隔（秒） */
  pollIntervalSeconds: number
  /** 每轮最多认领的任务数 */
  claimBatchSize: number
  /** 同时执行的任务数 */
  concurrency: number
  /** 启动后自动开始执行队列 */
  autoStartScheduler: boolean
  /** 模拟发布器的失败概率（0~1），用于演示失败重试链路 */
  mockFailureRate: number
}

// ==========================================================================
// 调度器与事件
// ==========================================================================

/** 调度器运行状态 */
export interface SchedulerStatus {
  running: boolean
  /** 正在执行的任务 ID */
  activeTaskIds: number[]
  /** 最近一次轮询时间 */
  lastPollAt: string | null
  /** 累计执行成功 / 失败次数（本次运行期间） */
  successCount: number
  failedCount: number
  /** 最近一次错误信息 */
  lastError: string
}

/** 调度器推送给渲染进程的事件类型 */
export type SchedulerEventType =
  | 'started'
  | 'stopped'
  | 'poll'
  | 'task-started'
  | 'task-succeeded'
  | 'task-failed'
  | 'task-retried'
  | 'error'

/** 调度器事件 */
export interface SchedulerEvent {
  type: SchedulerEventType
  /** 事件发生时间（ISO8601） */
  at: string
  /** 关联的任务 ID */
  taskId?: number
  /** 一句话描述，直接展示在界面上 */
  message: string
}

// ==========================================================================
// IPC 结果封装
// ==========================================================================

/** IPC 统一返回：不抛异常，改用结构化结果，渲染进程可拿到错误码与文案 */
export type IpcResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: { code: string; message: string } }

// ==========================================================================
// 预加载暴露给渲染进程的 API 契约
// ==========================================================================

/** 创建 / 更新账号的入参 */
export interface AccountPayload {
  platform: string
  nickname: string
  account_uid?: string
  status?: AccountStatus
  remark?: string
}

/** 批量创建发布任务的入参 */
export interface BatchCreatePayload {
  content_id: number
  account_ids: number[]
  /** 计划执行时间（ISO8601），留空表示立即执行 */
  scheduled_at?: string | null
  max_retries?: number
}

/** 发布任务列表查询条件 */
export interface TaskQuery {
  page?: number
  page_size?: number
  status?: PublishTaskStatus
  account_id?: number
  content_id?: number
}

/** 内容列表查询条件 */
export interface ContentQuery {
  page?: number
  page_size?: number
  status?: ContentStatus
  keyword?: string
}

/** 桌面客户端对外接口 */
export interface DesktopApi {
  config: {
    get(): Promise<IpcResult<AppConfig>>
    update(patch: Partial<AppConfig>): Promise<IpcResult<AppConfig>>
    /** 探测后端连通性 */
    ping(): Promise<IpcResult<{ version: string; database: string }>>
  }
  accounts: {
    list(): Promise<IpcResult<AccountListData>>
    create(payload: AccountPayload): Promise<IpcResult<Account>>
    update(id: number, payload: Partial<AccountPayload>): Promise<IpcResult<Account>>
    remove(id: number): Promise<IpcResult<{ id: number }>>
  }
  credentials: {
    status(): Promise<IpcResult<CredentialStoreStatus>>
    save(accountId: number, payload: CredentialPayload): Promise<IpcResult<CredentialSummary>>
    summary(accountId: number): Promise<IpcResult<CredentialSummary | null>>
    remove(accountId: number): Promise<IpcResult<{ accountId: number }>>
  }
  contents: {
    list(query?: ContentQuery): Promise<IpcResult<ListData<Content>>>
    get(id: number): Promise<IpcResult<Content>>
  }
  tasks: {
    list(query?: TaskQuery): Promise<IpcResult<ListData<PublishTask>>>
    batchCreate(payload: BatchCreatePayload): Promise<IpcResult<PublishTask[]>>
    cancel(id: number): Promise<IpcResult<PublishTask>>
    retry(id: number): Promise<IpcResult<PublishTask>>
    remove(id: number): Promise<IpcResult<{ id: number }>>
    statistics(): Promise<IpcResult<PublishTaskStatistics>>
  }
  scheduler: {
    start(): Promise<IpcResult<SchedulerStatus>>
    stop(): Promise<IpcResult<SchedulerStatus>>
    status(): Promise<IpcResult<SchedulerStatus>>
    /** 订阅调度器事件，返回取消订阅函数 */
    onEvent(listener: (event: SchedulerEvent) => void): () => void
  }
  system: {
    /** 用系统默认浏览器打开外链 */
    openExternal(url: string): Promise<IpcResult<{ opened: boolean }>>
    /** 打开本地数据目录（凭证与配置文件所在位置） */
    openDataDir(): Promise<IpcResult<{ opened: boolean }>>
  }
}
