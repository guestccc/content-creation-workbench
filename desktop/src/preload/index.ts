/**
 * 预加载脚本：渲染进程与主进程之间唯一的通道。
 *
 * 只暴露语义化的方法，不暴露 ipcRenderer 本身，
 * 渲染进程因此无法调用未注册的通道，也无法接触 Node API。
 */

import { contextBridge, ipcRenderer } from 'electron'

import type {
  AccountPayload,
  AppConfig,
  BatchCreatePayload,
  ContentQuery,
  CredentialPayload,
  DesktopApi,
  IpcResult,
  SchedulerEvent,
  TaskQuery
} from '@shared/types'

/** 调度器事件通道名，需与主进程保持一致 */
const SCHEDULER_EVENT_CHANNEL = 'scheduler:event'

/** 调用主进程处理器 */
function invoke<T>(channel: string, ...args: unknown[]): Promise<IpcResult<T>> {
  return ipcRenderer.invoke(channel, ...args) as Promise<IpcResult<T>>
}

/**
 * 实现对外接口。
 *
 * 显式标注为 DesktopApi 类型，任何一处遗漏或签名不一致都会在
 * 类型检查阶段暴露出来。
 */
const api: DesktopApi = {
  config: {
    get: () => invoke<AppConfig>('config:get'),
    update: (patch: Partial<AppConfig>) => invoke<AppConfig>('config:update', patch),
    ping: () => invoke<{ version: string; database: string }>('config:ping')
  },

  accounts: {
    list: () => invoke('accounts:list'),
    create: (payload: AccountPayload) => invoke('accounts:create', payload),
    update: (id: number, payload: Partial<AccountPayload>) =>
      invoke('accounts:update', id, payload),
    remove: (id: number) => invoke('accounts:remove', id)
  },

  credentials: {
    status: () => invoke('credentials:status'),
    save: (accountId: number, payload: CredentialPayload) =>
      invoke('credentials:save', accountId, payload),
    summary: (accountId: number) => invoke('credentials:summary', accountId),
    remove: (accountId: number) => invoke('credentials:remove', accountId)
  },

  contents: {
    list: (query?: ContentQuery) => invoke('contents:list', query),
    get: (id: number) => invoke('contents:get', id)
  },

  tasks: {
    list: (query?: TaskQuery) => invoke('tasks:list', query),
    batchCreate: (payload: BatchCreatePayload) => invoke('tasks:batchCreate', payload),
    cancel: (id: number) => invoke('tasks:cancel', id),
    retry: (id: number) => invoke('tasks:retry', id),
    remove: (id: number) => invoke('tasks:remove', id),
    statistics: () => invoke('tasks:statistics')
  },

  scheduler: {
    start: () => invoke('scheduler:start'),
    stop: () => invoke('scheduler:stop'),
    status: () => invoke('scheduler:status'),
    onEvent: (listener: (event: SchedulerEvent) => void) => {
      const handler = (_event: Electron.IpcRendererEvent, payload: SchedulerEvent): void => {
        listener(payload)
      }
      ipcRenderer.on(SCHEDULER_EVENT_CHANNEL, handler)
      // 返回取消订阅函数，供 React 在卸载时调用，避免监听器堆积
      return () => {
        ipcRenderer.removeListener(SCHEDULER_EVENT_CHANNEL, handler)
      }
    }
  },

  system: {
    openExternal: (url: string) => invoke('system:openExternal', url),
    openDataDir: () => invoke('system:openDataDir')
  }
}

contextBridge.exposeInMainWorld('api', api)
