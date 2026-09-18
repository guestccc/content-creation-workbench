/**
 * 本地目录选择器。
 *
 * 浏览器拿不到本地绝对路径，所以目录内容全部由后端 /fs/list 提供。
 * 支持：进入子目录、返回上一层、回到主目录、直接输入/粘贴路径、选定当前目录。
 */

import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Empty, Input, List, Modal, Space, Spin, Tag, Typography } from 'antd'

import { fetchDirectory } from '../api/filesystem'
import type { FsEntry, FsListData } from '../types/scene'
import { ApiError } from '../api/client'

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
}: DirectoryPickerProps) {
  const [data, setData] = useState<FsListData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string>('')
  /** 输入框里的路径（可能与已加载目录不同，回车才跳转） */
  const [inputPath, setInputPath] = useState<string>('')

  /** 加载指定目录；不传则加载后端默认（用户主目录） */
  const load = useCallback(async (path?: string) => {
    setLoading(true)
    setError('')
    try {
      const result = await fetchDirectory(path)
      setData(result)
      setInputPath(result.path)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '读取目录失败')
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

  const directories = (data?.entries ?? []).filter((entry) => entry.is_dir)

  /** 进入子目录 */
  const enter = (entry: FsEntry) => {
    void load(entry.path)
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
      width={640}
      okText="选定此目录"
      cancelText="取消"
      okButtonProps={{ disabled: !data }}
      onOk={confirm}
      destroyOnClose
    >
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
        <Button type="primary" ghost onClick={() => inputPath.trim() && load(inputPath.trim())}>
          前往
        </Button>
      </Space.Compact>

      {error && (
        <Alert type="error" message={error} showIcon style={{ marginBottom: 12 }} />
      )}

      <div style={{ maxHeight: 360, overflow: 'auto', minHeight: 200 }}>
        {loading ? (
          <div style={{ textAlign: 'center', padding: '60px 0' }}>
            <Spin />
          </div>
        ) : directories.length === 0 ? (
          <Empty
            description={data ? '这个目录下没有子目录，可以直接点「选定此目录」' : '暂无数据'}
          />
        ) : (
          <List
            size="small"
            dataSource={directories}
            renderItem={(entry) => (
              <List.Item
                style={{ cursor: 'pointer' }}
                onClick={() => enter(entry)}
                actions={[
                  <Button
                    key="open"
                    type="link"
                    size="small"
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
                  <span aria-hidden="true">📁</span>
                  <Text>{entry.name}</Text>
                </Space>
              </List.Item>
            )}
          />
        )}
      </div>

      <div style={{ marginTop: 12 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          当前目录：
        </Text>
        <Text code style={{ fontSize: 12 }}>
          {data?.path ?? '—'}
        </Text>
        {data?.truncated && (
          <Tag color="warning" style={{ marginLeft: 8 }}>
            子目录过多，已截断显示
          </Tag>
        )}
      </div>
    </Modal>
  )
}
