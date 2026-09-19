/**
 * Cookie 库的状态管理（本页私有，不进公共 hooks）。
 *
 * 只做五件事：
 * 1. 拉取 Cookie 列表（useAsyncData 静默失败）；
 * 2. 「从库选择」：选中后按 ID 拿完整串回填表单输入框（选择框只是插入器）；
 * 3. 「存到库」弹窗的开关、名称输入与保存（同名覆盖为更新）；
 * 4. 「管理库」弹窗的开关与删除；
 * 5. 「编辑」：按 ID 拿完整串挂载编辑弹窗，保存后刷新列表。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import type { UseApiMessageResult } from '../../hooks/useApiMessage'
import { useAsyncData } from '../../hooks/useAsyncData'
import {
  createCrawlCookie,
  deleteCrawlCookie,
  fetchCrawlCookie,
  fetchCrawlCookies,
  updateCrawlCookie,
} from '../../api/crawlCookie'
import type {
  CrawlCookie,
  CrawlCookieListData,
  CrawlCookieListItem,
} from '../../types/crawlCookie'
import type { CrawlPlatform } from '../../types/crawler'

/** hook 只用到 message 与 fail（contextHolder 由页面渲染，不进 hook） */
interface CookieLibraryApi {
  message: UseApiMessageResult['message']
  fail: UseApiMessageResult['fail']
}

export function useCookieLibrary(api: CookieLibraryApi) {
  const library = useAsyncData<CrawlCookieListData, undefined>({
    load: () => fetchCrawlCookies(),
    failMessage: '读取 Cookie 库失败',
    silent: true,
  })

  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [saveOpen, setSaveOpen] = useState(false)
  const [saveName, setSaveName] = useState('')
  const [managerOpen, setManagerOpen] = useState(false)
  const [saving, setSaving] = useState(false)

  const latest = useRef({ api })
  useEffect(() => {
    latest.current = { api }
  })

  /** 当前平台下的 Cookie 列表（选择框只显示同平台的） */
  const cookiesForPlatform = useCallback(
    (platform: CrawlPlatform): CrawlCookieListItem[] =>
      (library.data?.items ?? []).filter((item) => item.platform === platform),
    [library.data],
  )

  /** 从库选择：按 ID 拿完整串，返回给调用方回填输入框 */
  const select = useCallback(async (id: number): Promise<string | null> => {
    setSelectedId(id)
    try {
      const detail = await fetchCrawlCookie(id)
      return detail.cookie
    } catch (error) {
      latest.current.api.fail(error, '读取 Cookie 失败')
      return null
    }
  }, [])

  /** 打开存库弹窗（把输入框当前值配一个名字存起来） */
  const openSave = useCallback(() => {
    setSaveName('')
    setSaveOpen(true)
  }, [])

  const closeSave = useCallback(() => setSaveOpen(false), [])

  /** 确认保存：同平台同名 → 覆盖更新，否则新建 */
  const confirmSave = useCallback(
    async (platform: CrawlPlatform, cookie: string): Promise<boolean> => {
      const name = saveName.trim()
      if (!name) {
        latest.current.api.message.warning('请给 Cookie 起个名字（如「主号」）')
        return false
      }
      const existing = (library.data?.items ?? []).find(
        (item) => item.platform === platform && item.name === name,
      )
      setSaving(true)
      try {
        if (existing) {
          await updateCrawlCookie(existing.id, { name, cookie })
          latest.current.api.message.success(`已更新「${name}」`)
        } else {
          await createCrawlCookie({ platform, name, cookie })
          latest.current.api.message.success(`已保存「${name}」`)
        }
        await library.reload()
        setSaveOpen(false)
        return true
      } catch (error) {
        latest.current.api.fail(error, '保存 Cookie 失败')
        return false
      } finally {
        setSaving(false)
      }
    },
    [library, saveName],
  )

  /** 删除 */
  const remove = useCallback(
    async (id: number, name: string) => {
      try {
        await deleteCrawlCookie(id)
        latest.current.api.message.success(`已删除「${name}」`)
        if (selectedId === id) {
          setSelectedId(null)
        }
        await library.reload()
      } catch (error) {
        latest.current.api.fail(error, '删除 Cookie 失败')
      }
    },
    [library, selectedId],
  )

  /** 编辑弹窗的目标（null 表示没开）；列表项只有预览，编辑前按 ID 拿完整串 */
  const [editing, setEditing] = useState<CrawlCookie | null>(null)

  /** 从管理弹窗点「编辑」：拿完整串再挂载编辑弹窗 */
  const beginEdit = useCallback(async (id: number) => {
    try {
      const detail = await fetchCrawlCookie(id)
      setEditing(detail)
    } catch (error) {
      latest.current.api.fail(error, '读取 Cookie 失败')
    }
  }, [])

  const closeEdit = useCallback(() => setEditing(null), [])

  /** 编辑弹窗保存成功：提示 + 刷新列表 + 关弹窗（当前选中项的输入框回填由页面做） */
  const applyEdit = useCallback(
    async (updated: CrawlCookie) => {
      latest.current.api.message.success(`已更新「${updated.name}」`)
      setEditing(null)
      await library.reload()
    },
    [library],
  )

  return {
    items: library.data?.items ?? [],
    loading: library.loading,
    cookiesForPlatform,
    selectedId,
    setSelectedId,
    select,
    saveOpen,
    openSave,
    closeSave,
    saveName,
    setSaveName,
    confirmSave,
    saving,
    remove,
    managerOpen,
    setManagerOpen,
    editing,
    beginEdit,
    closeEdit,
    applyEdit,
  }
}
