/**
 * 内容相关的类型定义。
 *
 * 字段命名与后端 API 保持一致（snake_case），
 * 避免前后端字段映射带来的隐性错误，也便于对照接口文档排查问题。
 */

/** 内容状态 */
export type ContentStatus = 'draft' | 'reviewing' | 'published' | 'archived'

/** 内容实体 */
export interface Content {
  id: number
  title: string
  body: string
  platform: string
  status: ContentStatus
  tags: string[]
  author: string
  /** ISO8601 UTC 时间，如 2026-09-17T09:48:35.856343Z */
  created_at: string
  /** ISO8601 UTC 时间 */
  updated_at: string
}

/** 创建 / 更新内容的请求体 */
export interface ContentPayload {
  title: string
  body?: string
  platform?: string
  status?: ContentStatus
  tags?: string[]
  author?: string
}

/** 分页列表数据 */
export interface ContentListData {
  total: number
  page: number
  page_size: number
  items: Content[]
}

/** 内容统计信息 */
export interface ContentStatistics {
  total: number
  by_status: Record<ContentStatus, number>
}

/** 列表查询参数 */
export interface ContentQuery {
  page?: number
  page_size?: number
  status?: ContentStatus | ''
  platform?: string
  keyword?: string
}

/** 健康检查返回数据 */
export interface HealthData {
  status: string
  app_name: string
  version: string
  database: string
}

/** 状态展示配置：中文标签与主题色 */
export const STATUS_META: Record<ContentStatus, { label: string; color: string }> = {
  draft: { label: '草稿', color: '#8c8c8c' },
  reviewing: { label: '待审核', color: '#d48806' },
  published: { label: '已发布', color: '#389e0d' },
  archived: { label: '已归档', color: '#595959' },
}

/** 所有状态的展示顺序 */
export const STATUS_ORDER: ContentStatus[] = ['draft', 'reviewing', 'published', 'archived']

/** 目标平台候选项 */
export const PLATFORM_OPTIONS = ['抖音', '小红书', '视频号', '快手', 'B站', '公众号']
