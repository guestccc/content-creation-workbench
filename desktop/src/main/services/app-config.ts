/**
 * 本地应用配置的读写。
 *
 * 配置文件位于系统用户数据目录（macOS 下为
 * ~/Library/Application Support/content-workbench-desktop/config.json），
 * 不随应用包分发，也不会进入版本库。
 */

import { app } from 'electron'
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

import { CONFIG_LIMITS, DEFAULT_CONFIG } from '@shared/constants'
import type { AppConfig } from '@shared/types'

/** 配置文件名 */
const CONFIG_FILE = 'config.json'

/** 配置读取失败时的错误信息，供上层提示用户 */
export class ConfigError extends Error {}

/**
 * 把任意输入夹紧到合法区间。
 *
 * @param value 待校验的值
 * @param fallback 非法时的兜底值
 * @param range 合法区间
 */
function clampNumber(
  value: unknown,
  fallback: number,
  range: { min: number; max: number }
): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed)) {
    return fallback
  }
  return Math.min(range.max, Math.max(range.min, parsed))
}

/**
 * 校验并规范化后端地址。
 *
 * 只允许 http/https，避免误填 file:// 之类的协议被后续请求沿用。
 */
function normalizeBaseUrl(value: unknown): string {
  if (typeof value !== 'string') {
    return DEFAULT_CONFIG.backendBaseUrl
  }
  const trimmed = value.trim().replace(/\/+$/, '')
  if (!trimmed) {
    return DEFAULT_CONFIG.backendBaseUrl
  }
  try {
    const parsed = new URL(trimmed)
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return DEFAULT_CONFIG.backendBaseUrl
    }
    return trimmed
  } catch {
    return DEFAULT_CONFIG.backendBaseUrl
  }
}

/** 把任意来源的对象规范化为合法配置 */
function normalizeConfig(raw: unknown): AppConfig {
  const source = (raw ?? {}) as Partial<AppConfig>
  return {
    backendBaseUrl: normalizeBaseUrl(source.backendBaseUrl),
    pollIntervalSeconds: clampNumber(
      source.pollIntervalSeconds,
      DEFAULT_CONFIG.pollIntervalSeconds,
      CONFIG_LIMITS.pollIntervalSeconds
    ),
    claimBatchSize: clampNumber(
      source.claimBatchSize,
      DEFAULT_CONFIG.claimBatchSize,
      CONFIG_LIMITS.claimBatchSize
    ),
    concurrency: clampNumber(
      source.concurrency,
      DEFAULT_CONFIG.concurrency,
      CONFIG_LIMITS.concurrency
    ),
    autoStartScheduler:
      typeof source.autoStartScheduler === 'boolean'
        ? source.autoStartScheduler
        : DEFAULT_CONFIG.autoStartScheduler,
    mockFailureRate: clampNumber(
      source.mockFailureRate,
      DEFAULT_CONFIG.mockFailureRate,
      CONFIG_LIMITS.mockFailureRate
    )
  }
}

/** 应用配置仓库 */
class AppConfigStore {
  private cache: AppConfig | null = null

  /** 配置文件绝对路径 */
  get filePath(): string {
    return join(app.getPath('userData'), CONFIG_FILE)
  }

  /** 读取配置，首次调用时从磁盘加载，之后走内存缓存 */
  read(): AppConfig {
    if (this.cache) {
      return this.cache
    }

    const path = this.filePath
    if (!existsSync(path)) {
      this.cache = normalizeConfig(DEFAULT_CONFIG)
      return this.cache
    }

    try {
      const content = readFileSync(path, 'utf-8')
      this.cache = normalizeConfig(JSON.parse(content))
    } catch (error) {
      // 配置损坏时回退到默认值并保留原文件，避免用户数据被静默覆盖
      console.error('[config] 配置文件解析失败，已回退默认配置', error)
      this.cache = normalizeConfig(DEFAULT_CONFIG)
    }
    return this.cache
  }

  /**
   * 合并写入配置。
   *
   * 采用「先写临时文件再重命名」的方式，避免写入过程中断电导致配置文件半截。
   */
  update(patch: Partial<AppConfig>): AppConfig {
    const next = normalizeConfig({ ...this.read(), ...patch })

    const path = this.filePath
    try {
      mkdirSync(dirname(path), { recursive: true })
      const tempPath = `${path}.tmp`
      writeFileSync(tempPath, `${JSON.stringify(next, null, 2)}\n`, 'utf-8')
      renameSync(tempPath, path)
    } catch (error) {
      throw new ConfigError(
        `配置写入失败：${error instanceof Error ? error.message : String(error)}`
      )
    }

    this.cache = next
    return next
  }
}

/** 全局唯一的配置仓库实例 */
export const appConfig = new AppConfigStore()
