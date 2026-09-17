/**
 * 发布器注册表：按平台解析出对应的适配器。
 *
 * 接入一个新平台的步骤：
 * 1. 新建 src/main/publishers/<platform>.ts 实现 Publisher 接口；
 * 2. 在下面的 REAL_PUBLISHERS 数组里注册；
 * 其余代码无需改动。
 */

import { SUPPORTED_PLATFORMS } from '@shared/constants'

import { MockPublisher } from './mock'
import type { Publisher } from './types'

/**
 * 真实平台适配器列表。
 *
 * 目前为空：各平台的发布接口都需要正式授权与联调，
 * 在拿到接口文档之前先统一走模拟发布器，链路本身是完整可用的。
 */
const REAL_PUBLISHERS: Publisher[] = []

/** 兜底模拟发布器缓存，避免每次解析都新建实例 */
const mockCache = new Map<string, MockPublisher>()

/**
 * 解析某个平台应使用的发布器。
 *
 * @param platform 平台名称
 * @param mockFailureRate 模拟失败概率，仅对兜底实现生效
 */
export function resolvePublisher(platform: string, mockFailureRate = 0): Publisher {
  const normalized = platform.trim()
  const real = REAL_PUBLISHERS.find((publisher) => publisher.platforms.includes(normalized))
  if (real) {
    return real
  }

  const cached = mockCache.get(normalized)
  if (cached) {
    return cached
  }

  const fallback = new MockPublisher({ platform: normalized, failureRate: mockFailureRate })
  mockCache.set(normalized, fallback)
  return fallback
}

/** 列出所有可用适配器的展示信息，供设置页展示 */
export function listPublishers(): Array<{
  id: string
  displayName: string
  platforms: string[]
  isPlaceholder: boolean
  requiresCredential: boolean
}> {
  const real = REAL_PUBLISHERS.map((publisher) => ({
    id: publisher.id,
    displayName: publisher.displayName,
    platforms: [...publisher.platforms],
    isPlaceholder: publisher.isPlaceholder,
    requiresCredential: publisher.requiresCredential
  }))

  const mocked = SUPPORTED_PLATFORMS.filter(
    (platform) => !REAL_PUBLISHERS.some((publisher) => publisher.platforms.includes(platform))
  ).map((platform) => ({
    id: `mock:${platform}`,
    displayName: `${platform}（模拟发布）`,
    platforms: [platform],
    isPlaceholder: true,
    requiresCredential: false
  }))

  return [...real, ...mocked]
}
