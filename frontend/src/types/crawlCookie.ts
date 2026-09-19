/**
 * Cookie 库相关的类型定义。
 *
 * 字段命名与后端 API 保持一致（snake_case），不做 camelCase 转换。
 */

import type { CrawlPlatform } from './crawler'

/** 一条保存的 Cookie（详情接口返回，含完整串） */
export interface CrawlCookie {
  id: number
  platform: CrawlPlatform
  platform_label: string
  name: string
  cookie: string
  remark: string
  created_at: string
  updated_at: string
}

/** 列表项（只给截断预览，不给完整 cookie 串） */
export interface CrawlCookieListItem {
  id: number
  platform: CrawlPlatform
  platform_label: string
  name: string
  cookie_preview: string
  remark: string
  created_at: string
  updated_at: string
}

/** 保存 / 更新 Cookie 的请求体 */
export interface CrawlCookiePayload {
  platform: CrawlPlatform
  name: string
  cookie: string
  remark?: string
}

/** 列表接口返回（量少不分页） */
export interface CrawlCookieListData {
  total: number
  items: CrawlCookieListItem[]
}
