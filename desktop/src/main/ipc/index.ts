/**
 * IPC 处理器注册。
 *
 * 约定：处理器内部不抛异常，统一返回 IpcResult。
 * 这样渲染进程拿到的是结构化的错误码与中文提示，
 * 而不是 Electron 自动包装的「Error invoking remote method ...」。
 */

import { app, ipcMain, shell } from 'electron'

import type {
  AccountPayload,
  AppConfig,
  BatchCreatePayload,
  ContentQuery,
  CredentialPayload,
  IpcResult,
  TaskQuery
} from '@shared/types'

import { api } from '../services/api-client'
import { ConfigError, appConfig } from '../services/app-config'
import { CredentialError, credentialStore } from '../services/credential-store'
import { scheduler } from '../services/scheduler'

/**
 * 把处理器包成统一返回结构。
 *
 * @param handler 业务处理器，返回值会放进 data 字段
 */
function wrap<T>(handler: (...args: never[]) => Promise<T> | T) {
  return async (...args: unknown[]): Promise<IpcResult<T>> => {
    try {
      const data = await (handler as unknown as (...a: unknown[]) => Promise<T> | T)(...args)
      return { ok: true, data }
    } catch (error) {
      return { ok: false, error: toIpcError(error) }
    }
  }
}

/** 把任意异常转换为 IPC 错误对象 */
function toIpcError(error: unknown): { code: string; message: string } {
  if (error instanceof CredentialError) {
    return { code: 'CREDENTIAL_ERROR', message: error.message }
  }
  if (error instanceof ConfigError) {
    return { code: 'CONFIG_ERROR', message: error.message }
  }
  if (error instanceof Error) {
    // ApiError 自带业务错误码，这里沿用
    const code = (error as { code?: string }).code ?? 'INTERNAL_ERROR'
    return { code, message: error.message }
  }
  return { code: 'INTERNAL_ERROR', message: String(error) }
}

/** 校验 IPC 传入的正整数 ID */
function requireId(value: unknown, label: string): number {
  const parsed = Number(value)
  if (!Number.isInteger(parsed) || parsed <= 0) {
    throw new Error(`${label}不合法`)
  }
  return parsed
}

/** 校验并转换凭证载荷 */
function requireCredentialPayload(value: unknown): CredentialPayload {
  if (!value || typeof value !== 'object') {
    throw new CredentialError('凭证内容格式不正确')
  }
  const payload = value as CredentialPayload
  if (typeof payload.secret !== 'string') {
    throw new CredentialError('凭证内容格式不正确')
  }
  return payload
}

/** 注册全部 IPC 处理器 */
export function registerIpcHandlers(): void {
  // ------------------------------------------------------------------
  // 应用配置
  // ------------------------------------------------------------------
  ipcMain.handle(
    'config:get',
    wrap<AppConfig>(() => appConfig.read())
  )

  ipcMain.handle(
    'config:update',
    wrap<AppConfig>((patch) => {
      const next = appConfig.update((patch ?? {}) as Partial<AppConfig>)
      // 轮询间隔可能被改动，立即重启定时器让配置生效
      scheduler.applyConfigChange()
      return next
    })
  )

  ipcMain.handle(
    'config:ping',
    wrap(() => api.ping())
  )

  // ------------------------------------------------------------------
  // 账号
  // ------------------------------------------------------------------
  ipcMain.handle(
    'accounts:list',
    wrap(() => api.accounts.list())
  )

  ipcMain.handle(
    'accounts:create',
    wrap((payload) => api.accounts.create(payload as AccountPayload))
  )

  ipcMain.handle(
    'accounts:update',
    wrap((id, payload) =>
      api.accounts.update(requireId(id, '账号 ID'), (payload ?? {}) as Partial<AccountPayload>)
    )
  )

  ipcMain.handle(
    'accounts:remove',
    wrap(async (id) => {
      const accountId = requireId(id, '账号 ID')
      const result = await api.accounts.remove(accountId)
      // 账号在后端删除成功后，同步清掉本机保存的凭证，避免留下无主密文
      if (credentialStore.has(accountId)) {
        credentialStore.remove(accountId)
      }
      return result
    })
  )

  // ------------------------------------------------------------------
  // 凭证（仅本地，不经过网络）
  // ------------------------------------------------------------------
  ipcMain.handle(
    'credentials:status',
    wrap(() => credentialStore.status())
  )

  ipcMain.handle(
    'credentials:save',
    wrap((accountId, payload) =>
      credentialStore.save(requireId(accountId, '账号 ID'), requireCredentialPayload(payload))
    )
  )

  ipcMain.handle(
    'credentials:summary',
    wrap((accountId) => credentialStore.summary(requireId(accountId, '账号 ID')))
  )

  ipcMain.handle(
    'credentials:remove',
    wrap((accountId) => {
      const id = requireId(accountId, '账号 ID')
      credentialStore.remove(id)
      return { accountId: id }
    })
  )

  // ------------------------------------------------------------------
  // 内容
  // ------------------------------------------------------------------
  ipcMain.handle(
    'contents:list',
    wrap((query) => api.contents.list((query ?? {}) as ContentQuery))
  )

  ipcMain.handle(
    'contents:get',
    wrap((id) => api.contents.get(requireId(id, '内容 ID')))
  )

  // ------------------------------------------------------------------
  // 发布任务
  // ------------------------------------------------------------------
  ipcMain.handle(
    'tasks:list',
    wrap((query) => api.tasks.list((query ?? {}) as TaskQuery))
  )

  ipcMain.handle(
    'tasks:batchCreate',
    wrap((payload) => api.tasks.batchCreate(payload as BatchCreatePayload))
  )

  ipcMain.handle(
    'tasks:cancel',
    wrap((id) => api.tasks.cancel(requireId(id, '任务 ID')))
  )

  ipcMain.handle(
    'tasks:retry',
    wrap((id) => api.tasks.retry(requireId(id, '任务 ID')))
  )

  ipcMain.handle(
    'tasks:remove',
    wrap((id) => api.tasks.remove(requireId(id, '任务 ID')))
  )

  ipcMain.handle(
    'tasks:statistics',
    wrap(() => api.tasks.statistics())
  )

  // ------------------------------------------------------------------
  // 调度器
  // ------------------------------------------------------------------
  ipcMain.handle('scheduler:start', wrap(() => scheduler.start()))
  ipcMain.handle('scheduler:stop', wrap(() => scheduler.stop()))
  ipcMain.handle('scheduler:status', wrap(() => scheduler.status()))

  // ------------------------------------------------------------------
  // 系统
  // ------------------------------------------------------------------
  ipcMain.handle(
    'system:openExternal',
    wrap(async (url) => {
      const target = String(url ?? '')
      // 只允许打开 http/https，避免被诱导执行本地协议
      try {
        const parsed = new URL(target)
        if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
          return { opened: false }
        }
      } catch {
        return { opened: false }
      }
      await shell.openExternal(target)
      return { opened: true }
    })
  )

  ipcMain.handle(
    'system:openDataDir',
    wrap(async () => {
      const dir = app.getPath('userData')
      const error = await shell.openPath(dir)
      if (error) {
        throw new Error(`打开数据目录失败：${error}`)
      }
      return { opened: true }
    })
  )
}
