/**
 * 素材目录：选一个本地目录、列出里面的视频、勾选要处理的那几条。
 *
 * 浏览器拿不到本地绝对路径，所以「列目录」走的是后端 /fs/list。
 * 只有列目录是异步的，勾选与递归开关就是本地 state。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'

import { fetchDirectory } from '../api/filesystem'
import type { FsEntry } from '../types/scene'

export interface UseSourceDirResult {
  /** 已选中的素材目录（空串表示还没选） */
  path: string
  /** 设置素材目录；写成函数式更新（current => current || 默认值）才不会覆盖用户已经改过的选择 */
  setPath: Dispatch<SetStateAction<string>>
  /** 目录内容；null 表示没选目录或读取失败 */
  data: { path: string; entries: FsEntry[] } | null
  /** 目录下的视频文件 */
  videos: FsEntry[]
  /** 勾选的文件名；空数组表示「全部」 */
  selected: string[]
  setSelected: (names: string[]) => void
  /** 是否递归子目录 */
  recursive: boolean
  setRecursive: (recursive: boolean) => void
  /** 重新扫描当前目录，保留仍然存在的勾选 */
  rescan: () => void
  /** 「未勾选=全部」勾选框：勾上就清空已选（等价于全选） */
  toggleAll: (checked: boolean) => void
}

/**
 * @param fail 失败提示（useApiMessage().fail）
 */
export function useSourceDir(fail: (error: unknown, fallback: string) => void): UseSourceDirResult {
  const [path, setPath] = useState('')
  const [data, setData] = useState<{ path: string; entries: FsEntry[] } | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [recursive, setRecursive] = useState(false)

  /**
   * 列出目录下的视频文件。
   *
   * keepSelection 区分两种调用：切换目录时重新从零开始（清空勾选），
   * 手动「重新扫描」时保留勾选 —— 用户往往是拷完新素材顺手点一下，
   * 已经挑好的那几条不该被清掉（只保留确实还在目录里的）。
   */
  const scan = useCallback(
    async (target: string, keepSelection = false) => {
      try {
        const result = await fetchDirectory(target)
        setData({ path: result.path, entries: result.entries })
        if (keepSelection) {
          const available = new Set(
            result.entries.filter((entry) => entry.is_video).map((entry) => entry.name),
          )
          setSelected((current) => current.filter((name) => available.has(name)))
        } else {
          setSelected([])
        }
      } catch (error) {
        setData(null)
        fail(error, '读取目录失败')
      }
    },
    [fail],
  )

  // 目录变化时列出该目录下的视频文件
  useEffect(() => {
    if (!path) {
      setData(null)
      return
    }
    void scan(path)
  }, [path, scan])

  const videos = useMemo(
    () => (data?.entries ?? []).filter((entry) => entry.is_video),
    [data],
  )

  const rescan = useCallback(() => {
    if (path) {
      void scan(path, true)
    }
  }, [path, scan])

  const toggleAll = useCallback(
    (checked: boolean) => setSelected(checked ? [] : videos.map((video) => video.name)),
    [videos],
  )

  return {
    path,
    setPath,
    data,
    videos,
    selected,
    setSelected,
    recursive,
    setRecursive,
    rescan,
    toggleAll,
  }
}
