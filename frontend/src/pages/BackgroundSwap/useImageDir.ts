/**
 * 原图目录：选一个本地目录、列出里面的图片、勾选要换背景的那几张（本页私有）。
 *
 * 与公共的 useSourceDir 是同一套骨架，差别在三点，所以没有做成参数化的公用 hook：
 * - 按 `is_image` 过滤，不是 `is_video`（后端用的是图片白名单，见 /fs/list）；
 * - 不做递归：换背景是「挑一批图批量处理」，用户勾的就是眼前这一层；
 * - 没有视频预览弹窗，图片直接在列表里用缩略图看。
 * - 支持「从素材抓取带一批图过来」：目录与勾选由 prefill 填好 —— 进页面时带一次，
 *   在页面里选了抓取产物时再带一次（applyPrefill）。
 *
 * 只有本页用得到，按项目约定留在页面自己的文件夹里，不进 src/hooks/。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'

import { fetchDirectory } from '../../api/filesystem'
import type { BackgroundSwapPrefill } from '../../types/background'
import type { FsEntry } from '../../types/scene'

export interface UseImageDirResult {
  /** 已选中的原图目录（空串表示还没选） */
  path: string
  /** 设置原图目录；写成函数式更新（current => current || 默认值）才不会覆盖用户已经改过的选择 */
  setPath: Dispatch<SetStateAction<string>>
  /** 目录内容；null 表示没选目录或读取失败 */
  data: { path: string; entries: FsEntry[] } | null
  /** 目录下的图片文件 */
  images: FsEntry[]
  /** 勾选的图片文件名；空数组表示「全部」 */
  selected: string[]
  setSelected: (names: string[]) => void
  /** 重新扫描当前目录，保留仍然存在的勾选 */
  rescan: () => void
  /** 「未勾选=全部」勾选框：勾上就清空已选（等价于全选） */
  toggleAll: (checked: boolean) => void
  /**
   * 就地应用一份带入的图（目录 + 勾选）。给「在页面里选素材抓取产物」用 ——
   * 那条路径下页面已经挂载，构造函数里的 prefill 早就消费完了。
   */
  applyPrefill: (prefill: BackgroundSwapPrefill) => void
}

/**
 * @param fail 失败处理（useApiMessage().fail）
 * @param prefill 从别的页面带过来的原图（素材抓取的结果弹窗）；只在进入页面时
 *   生效一次，之后用户怎么改都不受影响。传 null 就是普通的「从零开始选」。
 *   页面里再带入的走 applyPrefill。
 */
export function useImageDir(
  fail: (error: unknown, fallback: string) => void,
  prefill: BackgroundSwapPrefill | null = null,
): UseImageDirResult {
  const [path, setPath] = useState(prefill?.inputPath ?? '')
  const [data, setData] = useState<{ path: string; entries: FsEntry[] } | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  /**
   * 待应用的带入勾选。放 ref 而不是 state：它只等**下一次扫描**来消费，
   * 扫描是异步的（目录一进页面就开始读），用 state 会让那次扫描读到旧值。
   */
  const pendingFiles = useRef<string[] | null>(prefill ? prefill.files : null)
  /**
   * 触发重扫的计数器。applyPrefill 带入的目录可能与当前目录**相同** ——
   * 这时 setPath 是同一个值、会被 React 直接忽略，光靠 path 这个依赖不会重扫，
   * 勾选也就落不了地。显式推一下计数器比让 effect 去比对象引用直白。
   */
  const [scanTick, setScanTick] = useState(0)
  /**
   * 扫描代次：只有最新一次扫描能落地结果，也只有它能消费 pendingFiles。
   * 否则连着选两次（或快速换目录）时，先发起的那次回来得晚就会把新的盖掉。
   */
  const scanToken = useRef(0)

  /**
   * 列出目录下的图片。
   *
   * 勾选分三种来路，优先级从高到低：
   * 1. 带入的清单（第一次扫描时消费掉，之后清空）；
   * 2. keepSelection：手动「重新扫描」时保留勾选 —— 用户往往是拷完新图顺手点
   *    一下，已经挑好的那几张不该被清掉（只保留确实还在目录里的）；
   * 3. 切换目录：从零开始。
   */
  const scan = useCallback(
    async (target: string, keepSelection = false) => {
      const token = (scanToken.current += 1)
      try {
        const result = await fetchDirectory(target)
        if (token !== scanToken.current) {
          return // 已经有更新的一次扫描在跑，这次的结果过期了
        }
        setData({ path: result.path, entries: result.entries })
        // 勾选只认目录里**列得出来的图片**（is_image 那批）：带入的清单来自
        // 「已下载的图片」，两边的白名单不完全一样（如 .gif 只在前者里），
        // 对不上就不勾 —— 免得出现「勾了 N 张、列表里只有 N-1 个框」
        const available = new Set(
          result.entries.filter((entry) => entry.is_image).map((entry) => entry.name),
        )
        const pending = pendingFiles.current
        if (pending !== null) {
          pendingFiles.current = null
          setSelected(pending.filter((name) => available.has(name)))
        } else if (keepSelection) {
          setSelected((current) => current.filter((name) => available.has(name)))
        } else {
          setSelected([])
        }
      } catch (error) {
        if (token !== scanToken.current) {
          return
        }
        // 这次没读成，带入的那批勾选也就作废了：抓取产物会被「删任务 + 清产物」
        // 整棵删掉，带入的目录随时可能不存在。不作废的话，用户下一次随手选个
        // 目录会突然套上一堆勾选 —— 那些文件名跟新目录毫无关系。
        pendingFiles.current = null
        setData(null)
        fail(error, '读取目录失败')
      }
    },
    [fail],
  )

  // 目录变化（或显式推一次重扫）时列出该目录下的图片
  useEffect(() => {
    if (!path) {
      setData(null)
      return
    }
    void scan(path)
  }, [path, scanTick, scan])

  const images = useMemo(
    () => (data?.entries ?? []).filter((entry) => entry.is_image),
    [data],
  )

  const rescan = useCallback(() => {
    if (path) {
      void scan(path, true)
    }
  }, [path, scan])

  const toggleAll = useCallback(
    (checked: boolean) => setSelected(checked ? [] : images.map((image) => image.name)),
    [images],
  )

  const applyPrefill = useCallback((next: BackgroundSwapPrefill) => {
    pendingFiles.current = next.files
    // 先清掉上一条笔记的勾选：同一个目录时扫描还要一个来回，这几帧里不该
    // 还挂着上一批勾选（列表没变、勾选却对不上，看着像点错了）
    setSelected([])
    // 同值 setState 会被 React 直接忽略，所以还要靠 scanTick 推一次重扫
    setPath((current) => (current === next.inputPath ? current : next.inputPath))
    setScanTick((tick) => tick + 1)
  }, [])

  return {
    path,
    setPath,
    data,
    images,
    selected,
    setSelected,
    rescan,
    toggleAll,
    applyPrefill,
  }
}
