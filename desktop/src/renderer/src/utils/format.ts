/** 界面展示用的格式化工具。 */

/** 把后端返回的 UTC ISO8601 时间格式化为本地时间 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return '—'
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return '—'
  }
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/** 相对时间描述，例如「3 分钟前」 */
export function formatRelative(value: string | null | undefined): string {
  if (!value) {
    return '—'
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return '—'
  }

  const diffSeconds = Math.round((Date.now() - date.getTime()) / 1000)
  if (diffSeconds < 5) return '刚刚'
  if (diffSeconds < 60) return `${diffSeconds} 秒前`
  if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)} 分钟前`
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)} 小时前`
  return `${Math.floor(diffSeconds / 86400)} 天前`
}

/**
 * 把本地 datetime-local 输入框的值转换为 UTC ISO8601 字符串。
 *
 * @returns 输入为空时返回 null
 */
export function localInputToUtcIso(value: string): string | null {
  if (!value) {
    return null
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return null
  }
  return date.toISOString()
}

/** 截断过长的文本 */
export function truncate(text: string, max = 40): string {
  if (!text) return ''
  return text.length <= max ? text : `${text.slice(0, max)}…`
}
