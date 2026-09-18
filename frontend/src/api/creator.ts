/**
 * 创作者主页库相关的接口调用。
 *
 * 每个函数对应后端一个接口，返回值已完成拆包（直接拿到 data）。
 * 列表量少不分页：平台 / 标签筛选由调用方拿到全量后自行过滤。
 */

import { del, get, post, put } from './client'
import type { Creator, CreatorListData, CreatorPayload } from '../types/creator'

/** 拉取创作者列表（platform / tag 过滤参数留给后端调试用，页面侧全量拉） */
export function fetchCreators(params?: { platform?: string; tag?: string }): Promise<CreatorListData> {
  return get<CreatorListData>('/creators', params ? { ...params } : undefined)
}

/** 新增创作者；同平台下主页重复时后端返回 409 */
export function createCreator(payload: CreatorPayload): Promise<Creator> {
  return post<Creator>('/creators', payload)
}

/** 编辑创作者（全字段提交，后端按 exclude_unset 跳过未传字段） */
export function updateCreator(id: number, payload: CreatorPayload): Promise<Creator> {
  return put<Creator>(`/creators/${id}`, payload)
}

/** 删除创作者（只删库里的记录，不影响历史抓取任务） */
export function deleteCreator(id: number): Promise<{ id: number }> {
  return del<{ id: number }>(`/creators/${id}`)
}
