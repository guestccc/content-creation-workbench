/**
 * 创作者主页库相关的类型定义。
 *
 * 字段命名与后端 API 保持一致（snake_case），不做 camelCase 转换。
 */

import type { CrawlPlatform } from './crawler'

/** 一位收藏的创作者 */
export interface Creator {
  id: number
  platform: CrawlPlatform
  platform_label: string
  name: string
  /** 主页链接或 ID：抓取时原样透传给 MC 的 --creator_id */
  homepage: string
  tags: string[]
  remark: string
  created_at: string
  updated_at: string
}

/** 新增 / 编辑创作者的请求体（tags 由后端清洗：≤10 个、每项 ≤20 字） */
export interface CreatorPayload {
  platform: CrawlPlatform
  name: string
  homepage: string
  tags?: string[]
  remark?: string
}

/** 列表接口返回（量少不分页，与后端 CreatorListData 对齐） */
export interface CreatorListData {
  total: number
  items: Creator[]
}
