/**
 * 创作者主页库页的数据与操作（本页私有，不进公共 hooks）。
 *
 * 列表用 useAsyncData 全量拉一次（静默失败，失败原因走 error 状态由页面
 * 渲染 Alert）—— 数据量是个位数到几十条，平台 / 标签筛选在页面侧做，
 * 不值得为筛选多跑一次接口。
 *
 * 新增 / 编辑的提交逻辑在 CreatorFormModal 里（照 ContentFormModal 的分工：
 * 弹窗自己管 saving 与错误展示），这里只管弹窗开关与删除。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { deleteCreator, fetchCreators } from '../../api/creator'
import { useAsyncData } from '../../hooks'
import type { UseApiMessageResult } from '../../hooks'
import type { Creator, CreatorListData } from '../../types/creator'

export function useCreatorLibrary(api: UseApiMessageResult) {
  // 回调进 ref（「最新 ref」模式）：对外函数引用恒定，页面不必写进依赖数组
  const latest = useRef(api)
  useEffect(() => {
    latest.current = api
  })

  const list = useAsyncData<CreatorListData, undefined>({
    load: () => fetchCreators(),
    failMessage: '读取创作者列表失败',
    silent: true,
  })

  // 弹窗状态：null 表示未打开；{ editing: null } 表示新建；{ editing: Creator } 表示编辑
  const [modal, setModal] = useState<{ editing: Creator | null } | null>(null)

  const openCreate = useCallback(() => setModal({ editing: null }), [])
  const openEdit = useCallback((creator: Creator) => setModal({ editing: creator }), [])
  const closeModal = useCallback(() => setModal(null), [])

  /** 弹窗保存成功后：关弹窗、提示、刷新列表 */
  const handleSaved = useCallback(
    (wasEditing: boolean) => {
      setModal(null)
      latest.current.message.success(wasEditing ? '保存成功' : '已添加创作者')
      void list.reload()
    },
    [list.reload],
  )

  /** 删除创作者（二次确认由页面做，这里只管调接口） */
  const remove = useCallback(
    async (creator: Creator): Promise<void> => {
      try {
        await deleteCreator(creator.id)
        latest.current.message.success(`已删除「${creator.name}」`)
        void list.reload()
      } catch (error) {
        latest.current.fail(error, '删除失败，请稍后重试')
      }
    },
    [list.reload],
  )

  return {
    creators: list.data?.items ?? [],
    loading: list.loading,
    /** 列表加载失败原因（成功时为空串），页面渲染成顶部 Alert */
    error: list.error,
    modal,
    openCreate,
    openEdit,
    closeModal,
    handleSaved,
    remove,
  }
}
