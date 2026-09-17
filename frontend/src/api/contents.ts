/**
 * 内容相关的接口调用。
 *
 * 每个函数对应后端一个接口，返回值已完成拆包（直接拿到 data）。
 */

import { del, get, post, put } from './client'
import type {
  Content,
  ContentListData,
  ContentPayload,
  ContentQuery,
  ContentStatistics,
  HealthData,
} from '../types/content'

/** 分页查询内容列表 */
export function fetchContents(query: ContentQuery = {}): Promise<ContentListData> {
  return get<ContentListData>('/contents', { ...query })
}

/** 获取内容详情 */
export function fetchContent(id: number): Promise<Content> {
  return get<Content>(`/contents/${id}`)
}

/** 创建内容 */
export function createContent(payload: ContentPayload): Promise<Content> {
  return post<Content>('/contents', payload)
}

/** 更新内容（仅传需要变更的字段） */
export function updateContent(id: number, payload: Partial<ContentPayload>): Promise<Content> {
  return put<Content>(`/contents/${id}`, payload)
}

/** 删除内容 */
export function deleteContent(id: number): Promise<{ id: number }> {
  return del<{ id: number }>(`/contents/${id}`)
}

/** 获取内容统计（总量与各状态分布） */
export function fetchStatistics(): Promise<ContentStatistics> {
  return get<ContentStatistics>('/contents/statistics')
}

/** 健康检查，用于首页展示后端连接状态 */
export function fetchHealth(): Promise<HealthData> {
  return get<HealthData>('/health')
}
