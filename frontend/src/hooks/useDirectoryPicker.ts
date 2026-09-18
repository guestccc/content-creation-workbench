/**
 * 目录选择弹窗的开关。
 *
 * 一个页面可能有多个用途的目录选择器（素材目录、输出目录、素材库目录），
 * 同一时刻只开一个，用 key 区分开的是哪一个。路径本身由页面持有，
 * 因为「选完之后落到哪个 state」是页面的事。
 */

import { useCallback, useState } from 'react'

export interface UseDirectoryPickerResult<K extends string> {
  /** 当前打开的是哪个选择器；null 表示都关着 */
  active: K | null
  open: (key: K) => void
  close: () => void
}

export function useDirectoryPicker<K extends string>(): UseDirectoryPickerResult<K> {
  const [active, setActive] = useState<K | null>(null)

  const open = useCallback((key: K) => setActive(key), [])
  const close = useCallback(() => setActive(null), [])

  return { active, open, close }
}
