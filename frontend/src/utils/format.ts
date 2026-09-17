/** 通用格式化工具。 */

/**
 * 把 ISO8601 UTC 时间格式化为本地时间字符串。
 *
 * 后端统一返回带 Z 后缀的时间，直接交给 Date 解析即可得到正确的本地时间。
 *
 * @param iso ISO8601 时间字符串
 * @returns 形如 2026-09-17 17:48 的本地时间；解析失败时原样返回
 */
export function formatDateTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) {
    return iso
  }

  const pad = (value: number): string => String(value).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

/** 把日期字符串转为 <input type="datetime-local"> 可用的值（本项目暂未使用，预留） */
export function toDateTimeLocal(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) {
    return ''
  }

  const pad = (value: number): string => String(value).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}
