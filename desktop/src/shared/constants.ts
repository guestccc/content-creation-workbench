/** 跨进程共用的常量与展示元数据。 */

import type { AccountStatus, PublishTaskStatus } from './types'

/** 默认配置：首次启动时写入本地配置文件 */
export const DEFAULT_CONFIG = {
  backendBaseUrl: 'http://127.0.0.1:8000',
  pollIntervalSeconds: 5,
  claimBatchSize: 5,
  concurrency: 2,
  autoStartScheduler: false,
  mockFailureRate: 0
} as const

/** 配置项的合法范围，主进程与界面共用，避免两处写死不同的边界 */
export const CONFIG_LIMITS = {
  pollIntervalSeconds: { min: 2, max: 300 },
  claimBatchSize: { min: 1, max: 20 },
  concurrency: { min: 1, max: 5 },
  mockFailureRate: { min: 0, max: 1 }
} as const

/** 发布任务状态展示元数据 */
export const TASK_STATUS_META: Record<PublishTaskStatus, { label: string; color: string }> = {
  pending: { label: '排队中', color: '#d97706' },
  running: { label: '发布中', color: '#2563eb' },
  success: { label: '已成功', color: '#16a34a' },
  failed: { label: '已失败', color: '#dc2626' },
  cancelled: { label: '已取消', color: '#64748b' }
}

/** 任务状态展示顺序 */
export const TASK_STATUS_ORDER: PublishTaskStatus[] = [
  'pending',
  'running',
  'success',
  'failed',
  'cancelled'
]

/** 账号状态展示元数据 */
export const ACCOUNT_STATUS_META: Record<AccountStatus, { label: string; color: string }> = {
  active: { label: '正常', color: '#16a34a' },
  disabled: { label: '已停用', color: '#64748b' },
  expired: { label: '已过期', color: '#dc2626' }
}

/** 已内置适配器的平台列表，其余平台会走模拟发布器 */
export const SUPPORTED_PLATFORMS = ['抖音', '小红书', '视频号', 'B站', '快手'] as const

/** 凭证类型展示元数据 */
export const CREDENTIAL_TYPE_META = {
  cookie: { label: 'Cookie', hint: '从浏览器复制的登录 Cookie 串' },
  token: { label: 'Token', hint: '开放平台签发的访问令牌' },
  password: { label: '账号密码', hint: '账号 + 密码，仅在本地加密保存' }
} as const
