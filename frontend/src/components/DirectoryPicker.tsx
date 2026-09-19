/**
 * 本地目录选择器。
 *
 * 浏览器拿不到本地绝对路径，所以目录内容全部由后端 /fs/list 提供。
 * 支持：进入子目录、返回上一层、回到主目录、直接输入/粘贴路径、选定当前目录。
 *
 * 左侧是收藏夹（后端全局一份，不区分弹窗用途）：浏览到常用目录点星收藏，
 * 之后任意弹窗里一键跳转。收藏加载失败只降级侧栏，浏览与选定不受影响。
 */

import { useCallback, useEffect, useState } from 'react'
import { CloseOutlined, FileOutlined, FolderOutlined, StarFilled, StarOutlined } from '@ant-design/icons'
import { Alert, Button, Empty, Flex, Input, List, Modal, Space, Spin, Tag, Typography } from 'antd'

import { useApiMessage } from '../hooks'
import { addFavorite, fetchDirectory, fetchFavorites, removeFavorite } from '../api/filesystem'
import { describeError } from '../api/client'
import type { FsEntry, FsFavorite, FsListData } from '../types/scene'

const { Text } = Typography

interface DirectoryPickerProps {
  /** 是否打开 */
  open: boolean
  /** 弹窗标题，如「选择素材目录」 */
  title: string
  /** 初始展示的目录；为空时从用户主目录开始 */
  initialPath?: string
  /** 关闭弹窗 */
  onClose: () => void
  /** 选定目录 */
  onSelect: (path: string) => void
  /**
   * 可选：允许选文件时给出后缀白名单（小写带点，如 ['.srt', '.mp4']）。
   * 不传时行为与原来完全一致（只列目录、只选目录）。
   */
  fileExtensions?: string[]
  /** 可选：选定一个文件（与 fileExtensions 配套；选定后弹窗自动关闭） */
  onSelectFile?: (path: string) => void
}

/** 收藏夹侧栏（纯展示 + 三个回调，只服务这一个组件，不抽出去） */
function FavoritesPanel({
  favorites,
  loading,
  unavailable,
  busyId,
  currentPath,
  onOpen,
  onDrop,
}: {
  favorites: FsFavorite[]
  loading: boolean
  unavailable: boolean
  /** 正在取消收藏的条目 id（给它的 ✕ 转圈） */
  busyId: string | null
  /** 当前浏览目录的规范化路径，命中的收藏高亮 */
  currentPath?: string
  onOpen: (item: FsFavorite) => void
  onDrop: (item: FsFavorite) => void
}) {
  return (
    <div style={{ flex: '0 0 200px', minWidth: 0 }}>
      <Flex justify="space-between" align="center" style={{ marginBottom: 4 }}>
        <Text strong style={{ fontSize: 13 }}>
          收藏目录
        </Text>
        {favorites.length > 0 && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {favorites.length}/50
          </Text>
        )}
      </Flex>
      {loading ? (
        <Flex justify="center" style={{ padding: '24px 0' }}>
          <Spin size="small" />
        </Flex>
      ) : unavailable ? (
        // 收藏加载失败只降级这一列，右侧浏览完全不受影响
        <Text type="secondary" style={{ fontSize: 12 }}>
          收藏列表暂不可用，不影响浏览
        </Text>
      ) : favorites.length === 0 ? (
        <Text type="secondary" style={{ fontSize: 12 }}>
          停在常用目录，点下方「收藏此目录」
        </Text>
      ) : (
        <div style={{ maxHeight: 420, overflow: 'auto' }}>
          <List
            size="small"
            dataSource={favorites}
            renderItem={(item) => {
              const active = item.path === currentPath
              return (
                <List.Item
                  onClick={() => onOpen(item)}
                  style={{
                    cursor: item.exists ? 'pointer' : 'not-allowed',
                    opacity: item.exists ? 1 : 0.45,
                    background: active ? 'var(--ant-color-fill-quaternary, #f5f5f5)' : undefined,
                    paddingLeft: 4,
                    paddingRight: 4,
                  }}
                  actions={[
                    <Button
                      key="drop"
                      type="text"
                      size="small"
                      icon={<CloseOutlined />}
                      loading={busyId === item.id}
                      title="取消收藏"
                      onClick={(event) => {
                        // 整行是「跳转」，✕ 是「取消收藏」，不阻止冒泡会先跳过去
                        event.stopPropagation()
                        onDrop(item)
                      }}
                    />,
                  ]}
                >
                  <Flex vertical style={{ minWidth: 0 }}>
                    <Space size={4}>
                      <FolderOutlined style={{ color: 'var(--color-warning)' }} />
                      <Text ellipsis style={{ fontSize: 13 }}>
                        {item.name}
                      </Text>
                      {!item.exists && <Tag color="default">失效</Tag>}
                    </Space>
                    <Text type="secondary" ellipsis style={{ fontSize: 11 }} title={item.path}>
                      {item.path}
                    </Text>
                  </Flex>
                </List.Item>
              )
            }}
          />
        </div>
      )}
    </div>
  )
}

/**
 * 目录选择弹窗。
 *
 * 只列目录（不展示文件内容，也不允许选中文件），
 * 「选定」按钮取的是当前所在目录的绝对路径。
 */
export default function DirectoryPicker({
  open,
  title,
  initialPath,
  onClose,
  onSelect,
  fileExtensions,
  onSelectFile,
}: DirectoryPickerProps) {
  const { fail, contextHolder } = useApiMessage()
  const [data, setData] = useState<FsListData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string>('')
  /** 输入框里的路径（可能与已加载目录不同，回车才跳转） */
  const [inputPath, setInputPath] = useState<string>('')

  const [favorites, setFavorites] = useState<FsFavorite[]>([])
  const [favLoading, setFavLoading] = useState(false)
  /** 收藏加载失败 → 侧栏降级成一行灰字 */
  const [favUnavailable, setFavUnavailable] = useState(false)
  /** 正在提交的收藏操作：条目 id（取消收藏）或 'toggle'（收藏/取消当前目录） */
  const [favBusy, setFavBusy] = useState<string | null>(null)

  /** 加载指定目录；不传则加载后端默认（用户主目录） */
  const load = useCallback(async (path?: string) => {
    setLoading(true)
    setError('')
    try {
      const result = await fetchDirectory(path)
      setData(result)
      setInputPath(result.path)
    } catch (err) {
      setError(describeError(err, '读取目录失败'))
    } finally {
      setLoading(false)
    }
  }, [])

  // 每次打开时按初始路径重新加载，避免上次浏览的位置残留
  useEffect(() => {
    if (open) {
      void load(initialPath || undefined)
    }
  }, [open, initialPath, load])

  // 收藏清单每次打开都重拉：同一页面可能有多个选择器实例（MixCut 的输出/素材），
  // 各自打开时都能看到最新状态，不需要全局 store。
  // 不并进上面的目录加载 effect —— 那个依赖 initialPath，MixCut 的 env 异步到达后
  // 会让它再跑一次，收藏会被白白拉两遍。
  useEffect(() => {
    if (!open) {
      return
    }
    let alive = true
    setFavLoading(true)
    setFavUnavailable(false)
    fetchFavorites()
      .then((list) => {
        if (alive) {
          setFavorites(list)
        }
      })
      .catch(() => {
        // 静默降级：只影响侧栏，不进 error、不打断浏览
        if (alive) {
          setFavorites([])
          setFavUnavailable(true)
        }
      })
      .finally(() => {
        if (alive) {
          setFavLoading(false)
        }
      })
    return () => {
      alive = false
    }
  }, [open])

  const directories = (data?.entries ?? []).filter((entry) => entry.is_dir)

  // 可选的文件挑选：只在调用方给了后缀白名单时出现（一键成品选字幕/成片用）；
  // 不传 fileExtensions 时 files 恒为空，列表与原来一模一样。
  const files = fileExtensions
    ? (data?.entries ?? []).filter(
        (entry) =>
          !entry.is_dir &&
          fileExtensions.some((ext) => entry.name.toLowerCase().endsWith(ext)),
      )
    : []

  /** 选定一个文件（文件行只出现在 file 模式下） */
  const pickFile = (entry: FsEntry) => {
    onSelectFile?.(entry.path)
    onClose()
  }

  // 判断「当前目录已收藏」必须用 canonical_path：path 保留用户写法
  // （小写盘符、含 ..），收藏条目是后端 resolve 过的，直接比字符串会假阴性
  const currentFavorite = data
    ? favorites.find((item) => item.path === data.canonical_path)
    : undefined

  /** 进入子目录 */
  const enter = (entry: FsEntry) => {
    void load(entry.path)
  }

  /** 收藏 / 取消收藏「当前目录」（与「选定此目录」作用于同一目录，语义并列） */
  const toggleFavorite = async () => {
    if (!data) {
      return
    }
    setFavBusy('toggle')
    try {
      if (currentFavorite) {
        await removeFavorite(currentFavorite.id)
        setFavorites((list) => list.filter((item) => item.id !== currentFavorite.id))
      } else {
        const created = await addFavorite(data.path)
        // 幂等 POST 可能返回已有条目（路径写法不同、resolve 后命中），按 id 去重
        setFavorites((list) =>
          list.some((item) => item.id === created.id) ? list : [...list, created],
        )
      }
    } catch (err) {
      fail(err, currentFavorite ? '取消收藏失败' : '收藏失败')
    } finally {
      setFavBusy(null)
    }
  }

  /** 侧栏条目：点名称跳转（失效条目不跳，但 ✕ 仍可用 —— 换盘符后得能删掉） */
  const openFavorite = (item: FsFavorite) => {
    if (item.exists) {
      void load(item.path)
    }
  }

  const dropFavorite = async (item: FsFavorite) => {
    setFavBusy(item.id)
    try {
      await removeFavorite(item.id)
      setFavorites((list) => list.filter((entry) => entry.id !== item.id))
    } catch (err) {
      fail(err, '取消收藏失败')
    } finally {
      setFavBusy(null)
    }
  }

  /** 确认选择当前目录 */
  const confirm = () => {
    if (data) {
      onSelect(data.path)
      onClose()
    }
  }

  return (
    <Modal
      open={open}
      title={title}
      onCancel={onClose}
      width={860}
      okText="选定此目录"
      cancelText="取消"
      okButtonProps={{ disabled: !data }}
      onOk={confirm}
      destroyOnHidden
    >
      {contextHolder}
      <Flex gap={12} align="flex-start">
        <FavoritesPanel
          favorites={favorites}
          loading={favLoading}
          unavailable={favUnavailable}
          busyId={favBusy}
          currentPath={data?.canonical_path}
          onOpen={openFavorite}
          onDrop={(item) => void dropFavorite(item)}
        />

        <div style={{ flex: 1, minWidth: 0 }}>
          <Space.Compact style={{ width: '100%', marginBottom: 12 }}>
            <Button onClick={() => data?.parent && load(data.parent)} disabled={!data?.parent}>
              上一层
            </Button>
            <Button onClick={() => load('~')}>主目录</Button>
            <Input
              value={inputPath}
              onChange={(event) => setInputPath(event.target.value)}
              onPressEnter={() => inputPath.trim() && load(inputPath.trim())}
              placeholder="也可以直接粘贴绝对路径，回车跳转"
              allowClear
            />
            <Button
              type="primary"
              ghost
              onClick={() => inputPath.trim() && load(inputPath.trim())}
            >
              前往
            </Button>
          </Space.Compact>

          {error && <Alert type="error" message={error} showIcon style={{ marginBottom: 12 }} />}

          <div style={{ maxHeight: 360, overflow: 'auto', minHeight: 200 }}>
            {loading ? (
              <div style={{ textAlign: 'center', padding: '60px 0' }}>
                <Spin />
              </div>
            ) : directories.length === 0 && files.length === 0 ? (
              <Empty
                description={
                  data
                    ? fileExtensions
                      ? '这个目录下没有子目录和可选的文件'
                      : '这个目录下没有子目录，可以直接点「选定此目录」'
                    : '暂无数据'
                }
              />
            ) : (
              <>
                <List
                  dataSource={directories}
                  renderItem={(entry) => (
                    <List.Item
                      style={{ cursor: 'pointer' }}
                      onClick={() => enter(entry)}
                      actions={[
                        <Button
                          key="open"
                          type="link"
                          style={{ padding: 0 }}
                          onClick={(event) => {
                            // 整行都能点，按钮只做视觉提示；不阻止冒泡会触发两次 enter
                            event.stopPropagation()
                            enter(entry)
                          }}
                        >
                          进入
                        </Button>,
                      ]}
                    >
                      <Space>
                        <FolderOutlined style={{ color: 'var(--color-warning)' }} />
                        <Text>{entry.name}</Text>
                      </Space>
                    </List.Item>
                  )}
                />
                {files.length > 0 && (
                  <List
                    dataSource={files}
                    renderItem={(entry) => (
                      <List.Item
                        style={{ cursor: 'pointer' }}
                        onClick={() => pickFile(entry)}
                        actions={[
                          <Button
                            key="pick"
                            type="link"
                            style={{ padding: 0 }}
                            onClick={(event) => {
                              event.stopPropagation()
                              pickFile(entry)
                            }}
                          >
                            选用
                          </Button>,
                        ]}
                      >
                        <Space>
                          <FileOutlined style={{ color: 'var(--color-info)' }} />
                          <Text>{entry.name}</Text>
                        </Space>
                      </List.Item>
                    )}
                  />
                )}
              </>
            )}
          </div>

          <Flex align="center" gap={8} style={{ marginTop: 12 }}>
            <Text type="secondary" style={{ fontSize: 12, flex: 'none' }}>
              当前目录：
            </Text>
            <Text code style={{ fontSize: 12 }} ellipsis={{ tooltip: data?.path }}>
              {data?.path ?? '—'}
            </Text>
            {data?.truncated && <Tag color="warning">子目录过多，已截断显示</Tag>}
            <Button
              type="text"
              size="small"
              style={{ marginLeft: 'auto' }}
              icon={
                currentFavorite ? <StarFilled style={{ color: '#faad14' }} /> : <StarOutlined />
              }
              loading={favBusy === 'toggle'}
              disabled={!data}
              onClick={() => void toggleFavorite()}
            >
              {currentFavorite ? '已收藏' : '收藏此目录'}
            </Button>
          </Flex>
        </div>
      </Flex>
    </Modal>
  )
}
