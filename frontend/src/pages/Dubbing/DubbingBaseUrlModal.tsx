/**
 * 「指定 Voicebox 服务地址」弹窗。
 *
 * 一般用不上（Voicebox 就在本机 127.0.0.1:17493），但远程 GPU 部署
 * （Voicebox 的 Remote Mode / Docker）时地址是变的 —— 页面上的输入框
 * 让用户直接指一下，比让他去改 backend/.env 靠谱。
 *
 * 保存由 useDubbingEnv.setBaseUrl 负责：后端写 .env + 原地热更新，
 * 返回值就是最新的自检结果，所以这里保存成功后只要关窗并让页面重新检测。
 */

import { useEffect, useState } from 'react'
import { Alert, Form, Input, Modal } from 'antd'

import type { VoiceboxEnvironment } from '../../types/voicebox'

interface DubbingBaseUrlModalProps {
  open: boolean
  /** 当前生效的环境（用来回填输入框与展示来源） */
  env: VoiceboxEnvironment | null
  /** 保存处理（useDubbingEnv.setBaseUrl）；返回是否成功 */
  onSave: (baseUrl: string) => Promise<boolean>
  onClose: () => void
}

interface FormValues {
  base_url: string
}

/** 地址来源的展示标签 */
const SOURCE_LABEL: Record<string, string> = {
  environment: '系统环境变量（优先级最高）',
  env_file: '.env 指定',
  default: '内置默认地址',
}

export default function DubbingBaseUrlModal({
  open,
  env,
  onSave,
  onClose,
}: DubbingBaseUrlModalProps) {
  const [form] = Form.useForm<FormValues>()
  const [saving, setSaving] = useState(false)

  // 每次打开回填当前生效地址（别处可能刚改过）
  useEffect(() => {
    if (open) {
      form.setFieldsValue({ base_url: env?.base_url ?? '' })
    }
  }, [open, env, form])

  const save = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      const ok = await onSave(values.base_url.trim())
      if (ok) {
        onClose()
      }
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={open}
      title="指定 Voicebox 服务地址"
      onCancel={onClose}
      okText="保存并重新检测"
      cancelText="取消"
      confirmLoading={saving}
      onOk={() => void save()}
      destroyOnHidden
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="Voicebox 装在别的机器上（远程 GPU）时才需要改"
        description="默认是本机 127.0.0.1:17493；改成任何本机可达的地址，例如 http://192.168.1.9:17493。留空恢复默认。"
      />
      <Form form={form} layout="vertical" disabled={saving}>
        <Form.Item
          name="base_url"
          label="服务地址"
          rules={[{ whitespace: true, message: '地址里不能只有空格' }]}
          extra={
            env
              ? `当前生效：${env.base_url}（来自${SOURCE_LABEL[env.base_url_source] ?? env.base_url_source}）`
              : '写回 backend/.env，改完不用重启服务'
          }
        >
          <Input placeholder="http://127.0.0.1:17493" allowClear autoComplete="off" />
        </Form.Item>
      </Form>
    </Modal>
  )
}
