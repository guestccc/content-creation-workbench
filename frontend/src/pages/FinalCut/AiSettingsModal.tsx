/**
 * 「AI 配置与口播语速」弹窗：base_url / 模型 / API key 三项 + 口播语速，
 * 保存一并写回 backend/.env。
 *
 * key 的读接口只给掩码（完整值不出后端），所以输入框留空 = 不改原 key；
 * 占位符显示掩码提示用户「已有一个 key 在位」。DeepSeek 的默认端点与模型
 * 由后端下发（首次打开时表单回填当前生效值），用户通常只需粘一次 key。
 *
 * **口播语速为什么放在这里**：它是新任务的**默认**语速（页面上每条任务可
 * 单独覆盖），藏在「DeepSeek」标题下会找不到。默认 5.0 字/秒只是经验值，
 * 各家音色快慢差得远 —— 自己量一次（「87 字念了 15 秒」）填进去才准，
 * 所以配了个换算助手：填字数与秒数点一下，算出语速填进主字段，不用按计算器。
 * 真实校验在后端（1.0–15.0，越界 400），前端这条只是提前拦一下。
 */

import { useEffect, useState } from 'react'
import { Alert, Button, Flex, Form, Input, InputNumber, Modal, Typography } from 'antd'

import { fetchAiSettings, updateAiSettings } from '../../api/finalcut'
import { describeError } from '../../api/client'
import type { UseApiMessageResult } from '../../hooks/useApiMessage'

const { Text } = Typography

interface AiSettingsModalProps {
  open: boolean
  /** 提示接口，直接传页面的 useApiMessage() 返回值 */
  api: UseApiMessageResult
  onClose: () => void
  /** 保存成功后调用（页面用它重新探测环境，好让新语速立刻生效） */
  onSaved: () => void
}

interface FormValues {
  base_url: string
  model: string
  api_key: string
  chars_per_second: number
}

export default function AiSettingsModal({ open, api, onClose, onSaved }: AiSettingsModalProps) {
  const [form] = Form.useForm<FormValues>()
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [keyMasked, setKeyMasked] = useState('')
  const [warning, setWarning] = useState('')
  const [rateWarning, setRateWarning] = useState('')
  const [rateDefault, setRateDefault] = useState(0)
  const [error, setError] = useState('')

  // 换算助手：与主字段同级但不受表单校验管辖（它不是要保存的值，只是个草稿）
  const [measuredChars, setMeasuredChars] = useState<number | null>(null)
  const [measuredSeconds, setMeasuredSeconds] = useState<number | null>(null)
  const [measuredHint, setMeasuredHint] = useState('')

  // 每次打开重新读当前生效值（别处可能刚改过）
  useEffect(() => {
    if (!open) {
      return
    }
    let alive = true
    setLoading(true)
    setError('')
    setMeasuredChars(null)
    setMeasuredSeconds(null)
    setMeasuredHint('')
    fetchAiSettings()
      .then((settings) => {
        if (!alive) {
          return
        }
        form.setFieldsValue({
          base_url: settings.base_url,
          model: settings.model,
          api_key: '',
          chars_per_second: settings.chars_per_second,
        })
        setKeyMasked(settings.api_key_present ? settings.api_key_masked : '')
        setWarning(settings.warning)
        setRateWarning(settings.chars_per_second_warning)
        setRateDefault(settings.chars_per_second_default)
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

  /** 凑不出正数秒数就不算（还没填完 / 填了 0）—— 按钮的 disabled 也按这个来 */
  const canMeasure =
    measuredChars !== null && measuredChars > 0 && measuredSeconds !== null && measuredSeconds > 0

  const applyMeasured = () => {
    if (!canMeasure) {
      return
    }
    // 与后端 normalize_chars_per_second 一致：四舍五入到两位小数
    const rate = Math.round((measuredChars / measuredSeconds) * 100) / 100
    form.setFieldsValue({ chars_per_second: rate })
    setMeasuredHint(`算出 ${rate} 字/秒，已填入上面（点「保存」才生效）`)
  }

  const save = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      await updateAiSettings({
        base_url: values.base_url.trim(),
        model: values.model.trim(),
        api_key: values.api_key.trim(),
        chars_per_second: values.chars_per_second,
      })
      api.message.success('配置已保存并生效')
      onSaved()
      onClose()
    } catch (err) {
      api.fail(err, '保存配置失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={open}
      title="AI 配置与口播语速"
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

        <Form.Item
          name="chars_per_second"
          label="默认口播语速（字/秒）"
          extra={`一秒念几个字。新任务的初始值（内置默认 ${rateDefault}），不影响已创建的任务 —— 每条任务的语速在页面上单独调；填你自己音色的实测值更准，文案的字数预算按它算。`}
          rules={[
            { required: true, message: '请填写口播语速' },
            { type: 'number', min: 1, max: 15, message: '语速要在 1.0–15.0 字/秒之间' },
          ]}
        >
          <InputNumber min={1} max={15} step={0.1} style={{ width: 160 }} />
        </Form.Item>

        <Form.Item label="按实测校准（可选）" style={{ marginBottom: 8 }}>
          <Flex vertical gap={6}>
            <Flex align="center" gap={6} wrap>
              <InputNumber
                min={1}
                placeholder="字数"
                style={{ width: 96 }}
                value={measuredChars}
                onChange={setMeasuredChars}
              />
              <Text type="secondary">字念了</Text>
              <InputNumber
                min={1}
                placeholder="秒数"
                style={{ width: 96 }}
                value={measuredSeconds}
                onChange={setMeasuredSeconds}
              />
              <Text type="secondary">秒</Text>
              <Button size="small" disabled={!canMeasure} onClick={applyMeasured}>
                算出语速
              </Button>
            </Flex>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {measuredHint ||
                '例：一段 87 字的稿子配音出来 15 秒，就填 87 和 15 —— 语速是 5.8 字/秒。'}
            </Text>
          </Flex>
        </Form.Item>
      </Form>
      {rateWarning && (
        <Alert type="warning" message={rateWarning} showIcon style={{ marginTop: 12 }} />
      )}
    </Modal>
  )
}
