import { useState } from 'react'
import { Alert, AutoComplete, Col, Form, Input, Modal, Row, Select } from 'antd'

import { ApiError } from '../api/client'
import { createContent, updateContent } from '../api/contents'
import type { Content, ContentPayload, ContentStatus } from '../types/content'
import { PLATFORM_OPTIONS, STATUS_META, STATUS_ORDER } from '../types/content'

interface ContentFormModalProps {
  /** 为 null 表示新建，否则为待编辑的内容 */
  editing: Content | null
  /** 关闭弹窗 */
  onClose: () => void
  /** 保存成功后回调，由父组件刷新列表 */
  onSaved: () => void
}

/** 表单内部状态：标签用字符串编辑，提交时再拆分为数组 */
interface FormValues {
  title: string
  platform: string
  status: ContentStatus
  author: string
  tags: string
  body: string
}

const EMPTY_FORM: FormValues = {
  title: '',
  platform: '',
  status: 'draft',
  author: '',
  tags: '',
  body: '',
}

/** 把内容实体转换为表单初值（标签用中文逗号连接，便于阅读） */
function toFormValues(content: Content): FormValues {
  return {
    title: content.title,
    platform: content.platform,
    status: content.status,
    author: content.author,
    tags: content.tags.join('，'),
    body: content.body,
  }
}

/** 把后端返回的字段级校验明细格式化为可读文本 */
function formatDetails(details: unknown): string {
  if (!Array.isArray(details)) {
    return ''
  }

  return details
    .map((item) => {
      if (item && typeof item === 'object' && 'field' in item && 'reason' in item) {
        const { field, reason } = item as { field: string; reason: string }
        return `${field}: ${reason}`
      }
      return ''
    })
    .filter(Boolean)
    .join('；')
}

/**
 * 内容创建 / 编辑弹窗。
 *
 * 同一个表单承担两种职责，由 editing 是否为空区分。
 * 弹窗由父组件按需挂载，所以表单初值每次都从 editing 重新算 —— 不必担心
 * 上一次编辑的内容残留在表单里。
 */
export default function ContentFormModal({ editing, onClose, onSaved }: ContentFormModalProps) {
  const [form] = Form.useForm<FormValues>()
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  /** 点「保存」：先过表单校验，再提交给后端 */
  const handleSubmit = async () => {
    let values: FormValues
    try {
      values = await form.validateFields()
    } catch {
      // 校验失败：antd 已在对应字段下给出提示，这里只要不关弹窗、不提交
      return
    }

    const payload: ContentPayload = {
      title: values.title.trim(),
      platform: values.platform?.trim() ?? '',
      status: values.status,
      author: values.author?.trim() ?? '',
      // 中英文逗号均作为分隔符，同时过滤掉空标签
      tags: (values.tags ?? '')
        .split(/[,，]/)
        .map((tag) => tag.trim())
        .filter(Boolean),
      body: values.body ?? '',
    }

    setSaving(true)
    setError('')

    try {
      if (editing) {
        await updateContent(editing.id, payload)
      } else {
        await createContent(payload)
      }
      onSaved()
    } catch (err) {
      if (err instanceof ApiError) {
        const detailText = formatDetails(err.details)
        setError(detailText ? `${err.message}（${detailText}）` : err.message)
      } else {
        setError('保存失败，请稍后重试')
      }
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open
      title={editing ? '编辑内容' : '新建内容'}
      width={640}
      okText="保存"
      cancelText="取消"
      confirmLoading={saving}
      // 关掉后销毁表单，下次打开是干净的
      destroyOnHidden
      onCancel={onClose}
      onOk={() => void handleSubmit()}
    >
      {error && (
        <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />
      )}

      <Form<FormValues>
        form={form}
        layout="vertical"
        initialValues={editing ? toFormValues(editing) : EMPTY_FORM}
        // 回车提交：与原来手写表单的行为一致
        onFinish={() => void handleSubmit()}
      >
        <Form.Item
          name="title"
          label="标题"
          rules={[{ required: true, message: '标题不能为空' }]}
        >
          <Input maxLength={200} placeholder="例如：秋季新品保温杯种草文案" />
        </Form.Item>

        {/* 平台 / 状态 / 作者并排一行，窄屏下各自换行 */}
        <Row gutter={16}>
          <Col xs={24} sm={10}>
            <Form.Item name="platform" label="目标平台">
              {/* AutoComplete：既能把常用平台当候选项，也允许直接输入别的 */}
              <AutoComplete
                options={PLATFORM_OPTIONS.map((platform) => ({ value: platform }))}
                placeholder="选择或直接输入"
              />
            </Form.Item>
          </Col>
          <Col xs={24} sm={7}>
            <Form.Item name="status" label="状态">
              <Select
                options={STATUS_ORDER.map((status) => ({
                  value: status,
                  label: STATUS_META[status].label,
                }))}
              />
            </Form.Item>
          </Col>
          <Col xs={24} sm={7}>
            <Form.Item name="author" label="作者">
              <Input maxLength={100} placeholder="创作者名称" />
            </Form.Item>
          </Col>
        </Row>

        <Form.Item
          name="tags"
          label="标签"
          extra="最多 10 个标签，重复标签会自动去除"
        >
          <Input placeholder="用逗号分隔，例如：保温杯，办公好物" />
        </Form.Item>

        <Form.Item name="body" label="正文">
          <Input.TextArea rows={7} placeholder="在这里撰写内容正文…" />
        </Form.Item>
      </Form>
    </Modal>
  )
}
