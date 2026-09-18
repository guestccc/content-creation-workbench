/**
 * 创作者新增 / 编辑弹窗（创作者主页页私有）。
 *
 * 全仓 antd Form 的既有写法只在 ContentFormModal，这里照同一套：
 * Form.useForm + 受控挂载（父组件按需渲染本组件）+ destroyOnHidden，
 * 弹窗每次打开表单初值都从 editing 重新算，不会残留上一次的输入。
 *
 * 错误展示在弹窗内的 Alert（409 重复、422 字段校验都要在弹窗不关的
 * 前提下可见），所以这里不走 useApiMessage 的 toast，与 ContentFormModal 一致。
 */

import { useState } from 'react'
import { Alert, Form, Input, Modal, Select } from 'antd'

import { ApiError } from '../../api/client'
import { createCreator, updateCreator } from '../../api/creator'
import { PLATFORM_META, PLATFORM_ORDER, PLATFORM_SPECS } from '../../types/crawler'
import type { CrawlPlatform } from '../../types/crawler'
import type { Creator, CreatorPayload } from '../../types/creator'

interface CreatorFormModalProps {
  /** 为 null 表示新建，否则为待编辑的创作者 */
  editing: Creator | null
  /** 新建时的默认平台（列表当前所在平台） */
  defaultPlatform: CrawlPlatform
  /** 关闭弹窗（未保存） */
  onClose: () => void
  /** 保存成功后回调，由父组件刷新列表 */
  onSaved: (wasEditing: boolean) => void
}

interface FormValues {
  platform: CrawlPlatform
  name: string
  homepage: string
  tags: string[]
  remark: string
}

/** 把创作者实体转换为表单初值 */
function toFormValues(creator: Creator): FormValues {
  return {
    platform: creator.platform,
    name: creator.name,
    homepage: creator.homepage,
    tags: creator.tags,
    remark: creator.remark,
  }
}

/** 把后端返回的字段级校验明细格式化为可读文本（照 ContentFormModal） */
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

export default function CreatorFormModal({
  editing,
  defaultPlatform,
  onClose,
  onSaved,
}: CreatorFormModalProps) {
  const [form] = Form.useForm<FormValues>()
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  // 主页输入框的提示随平台走：各平台主页形态差异大，文案来自平台能力矩阵
  const watchedPlatform = Form.useWatch('platform', form) as CrawlPlatform | undefined
  const spec = PLATFORM_SPECS[watchedPlatform ?? editing?.platform ?? defaultPlatform]

  /** 点「保存」：先过表单校验，再提交给后端 */
  const handleSubmit = async () => {
    let values: FormValues
    try {
      values = await form.validateFields()
    } catch {
      // 校验失败：antd 已在对应字段下给出提示，这里只要不关弹窗、不提交
      return
    }

    const payload: CreatorPayload = {
      platform: values.platform,
      name: values.name.trim(),
      homepage: values.homepage.trim(),
      tags: values.tags.map((tag) => tag.trim()).filter(Boolean),
      remark: values.remark.trim(),
    }

    setSaving(true)
    setError('')

    try {
      if (editing) {
        await updateCreator(editing.id, payload)
      } else {
        await createCreator(payload)
      }
      onSaved(editing !== null)
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
      title={editing ? '编辑创作者' : '添加创作者'}
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
        initialValues={editing ? toFormValues(editing) : {
          platform: defaultPlatform,
          name: '',
          homepage: '',
          tags: [],
          remark: '',
        }}
        // 回车提交，与 ContentFormModal 行为一致
        onFinish={() => void handleSubmit()}
      >
        <Form.Item name="platform" label="平台" rules={[{ required: true, message: '请选择平台' }]}>
          <Select
            options={PLATFORM_ORDER.map((p) => ({ value: p, label: PLATFORM_META[p].label }))}
            onChange={() => {
              // 各平台主页形态不通用（UID / sec_uid / 链接），换平台时清掉旧值
              form.setFieldsValue({ homepage: '' })
            }}
          />
        </Form.Item>

        <Form.Item
          name="name"
          label="名称"
          rules={[
            { required: true, whitespace: true, message: '名称不能为空' },
            { max: 50, message: '名称最多 50 字' },
          ]}
        >
          <Input maxLength={50} placeholder="博主昵称，或自己起个好记的名字" />
        </Form.Item>

        <Form.Item
          name="homepage"
          label="主页链接或 ID"
          rules={[
            { required: true, whitespace: true, message: '主页链接或 ID 不能为空' },
            { max: 500, message: '最多 500 字符' },
          ]}
          extra={spec.creatorHelp}
        >
          <Input maxLength={500} placeholder={spec.creatorPlaceholder} />
        </Form.Item>

        <Form.Item
          name="tags"
          label="标签"
          extra="回车或逗号确认，最多 10 个；列表页可按标签筛选"
        >
          <Select
            mode="tags"
            maxCount={10}
            tokenSeparators={[',', '，']}
            open={false}
            placeholder="例如：母婴、好物推荐"
          />
        </Form.Item>

        <Form.Item name="remark" label="备注" rules={[{ max: 200, message: '备注最多 200 字' }]}>
          <Input.TextArea rows={2} maxLength={200} placeholder="可选，比如：更新频率高、适合蹭热点" />
        </Form.Item>
      </Form>
    </Modal>
  )
}
