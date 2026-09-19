/**
 * Cookie 库相关的接口调用。
 *
 * 每个函数对应后端一个接口，返回值已完成拆包（直接拿到 data）。
 * 列表只给预览，完整 cookie 串用 fetchCrawlCookie 按 ID 拿。
 */

import { del, get, post, put } from './client'
import type {
  CrawlCookie,
  CrawlCookieListData,
  CrawlCookiePayload,
} from '../types/crawlCookie'

/** 拉取 Cookie 列表（量少不分页） */
export function fetchCrawlCookies(params?: { platform?: string }): Promise<CrawlCookieListData> {
  return get<CrawlCookieListData>('/crawl-cookies', params ? { ...params } : undefined)
}

/** 按 ID 拿完整 Cookie（抓取页选中后回填表单用） */
export function fetchCrawlCookie(id: number): Promise<CrawlCookie> {
  return get<CrawlCookie>(`/crawl-cookies/${id}`)
}

/** 保存一条 Cookie；同平台下名称重复时后端返回 409 */
export function createCrawlCookie(payload: CrawlCookiePayload): Promise<CrawlCookie> {
  return post<CrawlCookie>('/crawl-cookies', payload)
}

/** 更新 Cookie（部分字段，后端按 exclude_unset 跳过未传字段） */
export function updateCrawlCookie(id: number, payload: Partial<CrawlCookiePayload>): Promise<CrawlCookie> {
  return put<CrawlCookie>(`/crawl-cookies/${id}`, payload)
}

/** 删除 Cookie */
export function deleteCrawlCookie(id: number): Promise<{ id: number }> {
  return del<{ id: number }>(`/crawl-cookies/${id}`)
}
