/**
 * Cookie 库的编辑弹窗（素材抓取页私有）。
 *
 * 管理弹窗的列表项只有截断预览，编辑要用完整串，所以打开前由
 * useCookieLibrary 按 ID 取好详情再挂载本组件。写法与 CreatorFormModal
 * 同一套：Form.useForm + 受控挂载 + destroyOnHidden，弹窗每次打开表单
 * 初值都从 cookie 重新算，不残留上一次的输入。
 *
 * 错误展示在弹窗内的 Alert（409 同名冲突要在弹窗不关的前提下可见），
 * 不走 useApiMessage 的 toast，与 CreatorFormModal 一致。
 */

import { useState } from 'react'
import { Alert, Form, Input, Modal, Tag } from 'antd'

import { ApiError } from '../../api/client'
import { updateCrawlCookie } from '../../api/crawlCookie'
import type { CrawlCookie } from '../../types/crawlCookie'
import { PLATFORM_META } from '../../types/crawler'

interface CookieEditModalProps {
  /** 待编辑的 Cookie（完整串，来自详情接口） */
  cookie: CrawlCookie
  /** 关闭弹窗（未保存） */
  onClose: () => void
  /** 保存成功后回调，参数为更新后的实体（父组件刷新列表、回填输入框） */
  onSaved: (updated: CrawlCookie) => void
}

interface FormValues {
  name: string
  cookie: string
  remark: string
}

/** 把后端返回的字段级校验明细格式化为可读文本（照 CreatorFormModal） */
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

export default function CookieEditModal({ cookie, onClose, onSaved }: CookieEditModalProps) {
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

    setSaving(true)
    setError('')
    try {
      const updated = await updateCrawlCookie(cookie.id, {
        name: values.name.trim(),
        cookie: values.cookie.trim(),
        remark: values.remark.trim(),
      })
      onSaved(updated)
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
      title={`编辑 Cookie「${cookie.name}」`}
      width={560}
      okText="保存"
      cancelText="取消"
      confirmLoading={saving}
      // 关掉后销毁表单，下次打开是干净的
      destroyOnHidden
      onCancel={onClose}
      onOk={() => void handleSubmit()}
    >
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}

      <Form<FormValues>
        form={form}
        layout="vertical"
        initialValues={{
          name: cookie.name,
          cookie: cookie.cookie,
          remark: cookie.remark,
        }}
        // 回车提交，与 CreatorFormModal 行为一致
        onFinish={() => void handleSubmit()}
      >
        {/* 平台是这条 Cookie 的归属（选择框按平台过滤），不允许改错库 */}
        <Form.Item label="平台">
          <Tag color={PLATFORM_META[cookie.platform].color} style={{ marginInlineEnd: 0 }}>
            {PLATFORM_META[cookie.platform].label}
          </Tag>
        </Form.Item>

        <Form.Item
          name="name"
          label="名称"
          rules={[
            { required: true, whitespace: true, message: '名称不能为空' },
            { max: 50, message: '名称最多 50 字' },
          ]}
        >
          <Input maxLength={50} placeholder="如「主号」「小号」" />
        </Form.Item>

        <Form.Item
          name="cookie"
          label="Cookie"
          rules={[
            { required: true, whitespace: true, message: 'Cookie 不能为空' },
          ]}
          extra="Cookie 失效后在这里粘贴新串覆盖即可"
        >
          <Input.TextArea rows={4} placeholder="登录后浏览器里的 Cookie 串" />
        </Form.Item>

        <Form.Item name="remark" label="备注" rules={[{ max: 200, message: '备注最多 200 字' }]}>
          <Input.TextArea rows={2} maxLength={200} placeholder="可选" />
        </Form.Item>
      </Form>
    </Modal>
  )
}
