/**
 * 平台登录凭证的本地加密存储。
 *
 * 安全设计（这是本项目最重要的一条约束）：
 * 1. 明文凭证只存在于内存中，落盘前一律经过 Electron safeStorage 加密，
 *    macOS 上由系统钥匙串托管密钥；
 * 2. 凭证永不经过后端接口，也永不写入数据库，后端只保存账号元信息；
 * 3. 系统加密不可用时直接拒绝保存，绝不退化成明文存储；
 * 4. 日志中只出现脱敏后的凭证预览。
 */

import { app, safeStorage } from 'electron'
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

import type {
  CredentialPayload,
  CredentialStoreStatus,
  CredentialSummary,
  CredentialType
} from '@shared/types'

/** 凭证文件名 */
const CREDENTIAL_FILE = 'credentials.json'

/** 文件格式版本，便于后续做迁移 */
const FILE_VERSION = 1

/** 落盘的单条凭证记录（密文） */
interface StoredCredential {
  /** base64 编码的密文 */
  cipher: string
  /** 非敏感的元信息，便于在不解密的情况下展示 */
  type: CredentialType
  username?: string
  expiresAt?: string | null
  updatedAt: string
}

/** 凭证文件结构 */
interface CredentialFile {
  version: number
  entries: Record<string, StoredCredential>
}

/** 凭证操作失败 */
export class CredentialError extends Error {}

/**
 * 生成脱敏预览：保留首尾各 4 位，中间用星号代替。
 *
 * 长度不足 8 位时全部打码，避免因为凭证太短而泄露大部分内容。
 */
export function maskSecret(secret: string): string {
  if (!secret) {
    return ''
  }
  if (secret.length <= 8) {
    return '*'.repeat(secret.length)
  }
  return `${secret.slice(0, 4)}****${secret.slice(-4)}`
}

/** 校验凭证载荷，拒绝明显无效的输入 */
function assertPayload(payload: CredentialPayload): void {
  if (!payload || typeof payload !== 'object') {
    throw new CredentialError('凭证内容格式不正确')
  }
  if (!['cookie', 'token', 'password'].includes(payload.type)) {
    throw new CredentialError('凭证类型不支持')
  }
  if (typeof payload.secret !== 'string' || !payload.secret.trim()) {
    throw new CredentialError('凭证内容不能为空')
  }
  if (payload.type === 'password' && !payload.username?.trim()) {
    throw new CredentialError('账号密码类型的凭证必须填写账号名')
  }
}

/** 凭证仓库 */
class CredentialStore {
  private cache: CredentialFile | null = null

  /** 凭证文件绝对路径 */
  get filePath(): string {
    return join(app.getPath('userData'), CREDENTIAL_FILE)
  }

  /** 系统加密是否可用，不可用时给出可读原因 */
  status(): CredentialStoreStatus {
    const available = safeStorage.isEncryptionAvailable()
    return {
      available,
      message: available
        ? '系统加密可用，凭证将以密文保存于本地'
        : '当前系统的加密能力不可用，出于安全考虑将拒绝保存凭证',
      savedAccountIds: Object.keys(this.readFile().entries)
        .map((key) => Number(key))
        .filter((id) => Number.isInteger(id))
    }
  }

  /**
   * 保存（或覆盖）某个账号的凭证。
   *
   * @param accountId 账号 ID，与后端账号记录一一对应
   * @param payload 明文凭证
   * @returns 脱敏后的摘要，供界面展示
   */
  save(accountId: number, payload: CredentialPayload): CredentialSummary {
    if (!Number.isInteger(accountId) || accountId <= 0) {
      throw new CredentialError('账号 ID 不合法')
    }
    assertPayload(payload)

    if (!safeStorage.isEncryptionAvailable()) {
      throw new CredentialError(
        '系统加密能力不可用，已拒绝保存明文凭证。请检查系统钥匙串后重试。'
      )
    }

    const plaintext = JSON.stringify({
      type: payload.type,
      secret: payload.secret,
      username: payload.username ?? '',
      expiresAt: payload.expiresAt ?? null,
      extra: payload.extra ?? {}
    })

    let cipher: string
    try {
      cipher = safeStorage.encryptString(plaintext).toString('base64')
    } catch (error) {
      throw new CredentialError(
        `凭证加密失败：${error instanceof Error ? error.message : String(error)}`
      )
    }

    const updatedAt = new Date().toISOString()
    const file = this.readFile()
    file.entries[String(accountId)] = {
      cipher,
      type: payload.type,
      username: payload.username,
      expiresAt: payload.expiresAt ?? null,
      updatedAt
    }
    this.writeFile(file)

    console.info('[credentials] 凭证已加密保存 | accountId=%s', accountId)

    return {
      accountId,
      type: payload.type,
      maskedSecret: maskSecret(payload.secret),
      username: payload.username,
      expiresAt: payload.expiresAt ?? null,
      updatedAt
    }
  }

  /**
   * 读取并解密凭证。
   *
   * @returns 凭证不存在时返回 null
   */
  load(accountId: number): CredentialPayload | null {
    const entry = this.readFile().entries[String(accountId)]
    if (!entry) {
      return null
    }
    if (!safeStorage.isEncryptionAvailable()) {
      throw new CredentialError('系统加密能力不可用，无法解密已保存的凭证')
    }

    try {
      const plaintext = safeStorage.decryptString(Buffer.from(entry.cipher, 'base64'))
      return JSON.parse(plaintext) as CredentialPayload
    } catch (error) {
      // 解密失败通常意味着钥匙串条目被清除，或换了机器 / 用户
      console.error('[credentials] 凭证解密失败 | accountId=%s', accountId)
      throw new CredentialError(
        `凭证解密失败，可能是系统钥匙串条目已被清除，请重新保存：${
          error instanceof Error ? error.message : String(error)
        }`
      )
    }
  }

  /** 获取脱敏摘要，不会解密出完整凭证 */
  summary(accountId: number): CredentialSummary | null {
    const entry = this.readFile().entries[String(accountId)]
    if (!entry) {
      return null
    }

    let maskedSecret = ''
    try {
      const payload = this.load(accountId)
      maskedSecret = payload ? maskSecret(payload.secret) : ''
    } catch {
      // 解密失败不影响展示元信息，此时预览留空，界面会提示需要重新保存
      maskedSecret = ''
    }

    return {
      accountId,
      type: entry.type,
      maskedSecret,
      username: entry.username,
      expiresAt: entry.expiresAt ?? null,
      updatedAt: entry.updatedAt
    }
  }

  /** 删除某账号的凭证 */
  remove(accountId: number): void {
    const file = this.readFile()
    if (!file.entries[String(accountId)]) {
      return
    }
    delete file.entries[String(accountId)]
    this.writeFile(file)
    console.info('[credentials] 凭证已删除 | accountId=%s', accountId)
  }

  /** 判断某账号是否已保存凭证 */
  has(accountId: number): boolean {
    return Boolean(this.readFile().entries[String(accountId)])
  }

  // ------------------------------------------------------------------
  // 文件读写
  // ------------------------------------------------------------------

  private readFile(): CredentialFile {
    if (this.cache) {
      return this.cache
    }

    const path = this.filePath
    if (!existsSync(path)) {
      this.cache = { version: FILE_VERSION, entries: {} }
      return this.cache
    }

    try {
      const parsed = JSON.parse(readFileSync(path, 'utf-8')) as Partial<CredentialFile>
      this.cache = {
        version: typeof parsed.version === 'number' ? parsed.version : FILE_VERSION,
        entries: parsed.entries && typeof parsed.entries === 'object' ? parsed.entries : {}
      }
    } catch (error) {
      // 文件损坏时以空集合启动；不覆盖原文件，保留人工排查的可能
      console.error('[credentials] 凭证文件解析失败', error)
      this.cache = { version: FILE_VERSION, entries: {} }
    }
    return this.cache
  }

  private writeFile(file: CredentialFile): void {
    const path = this.filePath
    try {
      mkdirSync(dirname(path), { recursive: true })
      const tempPath = `${path}.tmp`
      writeFileSync(tempPath, `${JSON.stringify(file, null, 2)}\n`, { encoding: 'utf-8', mode: 0o600 })
      renameSync(tempPath, path)
    } catch (error) {
      throw new CredentialError(
        `凭证文件写入失败：${error instanceof Error ? error.message : String(error)}`
      )
    }
    this.cache = file
  }
}

/** 全局唯一的凭证仓库实例 */
export const credentialStore = new CredentialStore()
