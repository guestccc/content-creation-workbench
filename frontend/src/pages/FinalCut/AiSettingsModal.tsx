/**
 * 「AI 配置」弹窗：base_url / 模型 / API key 三项，保存写回 backend/.env。
 *
 * key 的读接口只给掩码（完整值不出后端），所以输入框留空 = 不改原 key；
 * 占位符显示掩码提示用户「已有一个 key 在位」。DeepSeek 的默认端点与模型
 * 由后端下发（首次打开时表单回填当前生效值），用户通常只需粘一次 key。
 */

import { useEffect, useState } from 'react'
import { Alert, Form, Input, Modal } from 'antd'

import { fetchAiSettings, updateAiSettings } from '../../api/finalcut'
import { describeError } from '../../api/client'
import type { UseApiMessageResult } from '../../hooks/useApiMessage'

interface AiSettingsModalProps {
  open: boolean
  /** 提示接口，直接传页面的 useApiMessage() 返回值 */
  api: UseApiMessageResult
  onClose: () => void
  /** 保存成功后调用（页面用它重新探测环境） */
  onSaved: () => void
}

interface FormValues {
  base_url: string
  model: string
  api_key: string
}

export default function AiSettingsModal({ open, api, onClose, onSaved }: AiSettingsModalProps) {
  const [form] = Form.useForm<FormValues>()
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [keyMasked, setKeyMasked] = useState('')
  const [warning, setWarning] = useState('')
  const [error, setError] = useState('')

  // 每次打开重新读当前生效值（别处可能刚改过）
  useEffect(() => {
    if (!open) {
      return
    }
    let alive = true
    setLoading(true)
    setError('')
    fetchAiSettings()
      .then((settings) => {
        if (!alive) {
          return
        }
        form.setFieldsValue({
          base_url: settings.base_url,
          model: settings.model,
          api_key: '',
        })
        setKeyMasked(settings.api_key_present ? settings.api_key_masked : '')
        setWarning(settings.warning)
      })
      .catch((err) => {
        if (alive) {
          setError(describeError(err, '读取 AI 配置失败'))
        }
      })
      .finally(() => {
        if (alive) {
          setLoading(false)
        }
      })
    return () => {
      alive = false
    }
  }, [open, form])

  const save = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      await updateAiSettings({
        base_url: values.base_url.trim(),
        model: values.model.trim(),
        api_key: values.api_key.trim(),
      })
      api.message.success('AI 配置已保存并生效')
      onSaved()
      onClose()
    } catch (err) {
      api.fail(err, '保存 AI 配置失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={open}
      title="AI 配置（DeepSeek）"
      onCancel={onClose}
      okText="保存"
      cancelText="取消"
      confirmLoading={saving}
      onOk={() => void save()}
      destroyOnHidden
    >
      {error && <Alert type="error" message={error} showIcon style={{ marginBottom: 12 }} />}
      {warning && <Alert type="warning" message={warning} showIcon style={{ marginBottom: 12 }} />}
      <Form form={form} layout="vertical" disabled={loading || saving}>
        <Form.Item
          name="base_url"
          label="API 端点"
          rules={[{ required: true, whitespace: true, message: '请填写 API 端点' }]}
        >
          <Input placeholder="https://api.deepseek.com/v1" />
        </Form.Item>
        <Form.Item
          name="model"
          label="模型"
          rules={[{ required: true, whitespace: true, message: '请填写模型名' }]}
        >
          <Input placeholder="deepseek-chat" />
        </Form.Item>
        <Form.Item
          name="api_key"
          label="API key"
          extra={keyMasked ? `当前已配置 ${keyMasked}，留空表示不修改` : '写回 backend/.env，长期生效'}
        >
          <Input.Password
            placeholder={keyMasked ? `${keyMasked}（留空保持不变）` : 'sk-...'}
            autoComplete="off"
          />
        </Form.Item>
      </Form>
    </Modal>
  )
}
