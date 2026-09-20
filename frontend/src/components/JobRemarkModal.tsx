/**
 * 任务备注的编辑弹窗（六个历史任务列表共用）。
 *
 * 打开它的入口是历史表里的「备注」列（见 jobColumns 的 jobRemarkColumn），
 * 传进来的是列表里那一行本身 —— 备注就在行上，不必为编辑再取一次详情。
 *
 * 各任务域的保存接口不同（/scene、/subtitle、/mix……），所以接口函数由调用方
 * 以 save 传进来，本组件不认识任何一个域。
 *
 * 写法与 CookieEditModal 同一套：Form.useForm + 受控挂载 + destroyOnHidden，
 * 弹窗每次打开表单初值都从 job 重新算，不残留上一次的输入；保存失败时弹窗
 * 不关、输入不丢，错误展示在弹窗内的 Alert（失败的语境在弹窗里）。
 */

import { useState } from 'react'
import { Alert, Form, Input, Modal } from 'antd'

import { describeError } from '../api/client'
import type { JobRowRemark } from './jobColumns'

export interface JobRemarkModalProps {
  /** 待编辑的任务（历史表里那一行） */
  job: JobRowRemark
  /** 保存接口（各任务域不同）；remark 为空串表示清空备注 */
  save: (jobId: number, remark: string) => Promise<unknown>
  /** 关闭弹窗（未保存） */
  onClose: () => void
  /** 保存成功：关弹窗 + 提示 + 刷新列表，由调用方做 */
  onSaved: () => void
}

interface FormValues {
  remark: string
}

export default function JobRemarkModal({ job, save, onClose, onSaved }: JobRemarkModalProps) {
  const [form] = Form.useForm<FormValues>()
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  /** 点「保存」：先过表单校验，再提交给后端 */
  const handleSubmit = async () => {
    let values: FormValues
    try {
      values = await form.validateFields()
    } catch {
      // 校验失败：antd 已在字段下给出提示，这里只要不关弹窗、不提交
      return
    }

    setSaving(true)
    setError('')
    try {
      // trim 与后端的去空白保持一致，避免「看着是空的其实不是」的备注存进去
      await save(job.id, (values.remark ?? '').trim())
      onSaved()
    } catch (err) {
      // 统一走 describeError：ApiError 用后端原文，其余用兜底文案
      setError(describeError(err, '保存备注失败，请稍后重试'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open
      title={`备注任务 #${job.id}`}
      width={480}
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
        initialValues={{ remark: job.remark ?? '' }}
        // 回车提交，与其余编辑弹窗一致
        onFinish={() => void handleSubmit()}
      >
        <Form.Item
          name="remark"
          label="备注"
          rules={[{ max: 200, message: '备注最多 200 字' }]}
          extra="清空后保存即删除备注"
        >
          <Input.TextArea
            rows={4}
            maxLength={200}
            showCount
            placeholder="给这条任务做个记号，如「客户 A 的那版」"
          />
        </Form.Item>
      </Form>
    </Modal>
  )
}
