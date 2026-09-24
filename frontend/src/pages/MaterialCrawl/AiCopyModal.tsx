/**
 * 「AI 文案」弹窗：把这条笔记的原文交给 AI，给出小红书 / 抖音两套候选标题与简介
 * （各 5 条标题 + 3 条简介），逐条可复制 —— 用户拿去发这两个平台。
 *
 * 状态在页面侧的 useAiCopy 里（读库 / 流式生成 / 换一批 / 过期帧丢弃都在那边），
 * 这里只负责把四种状态摆出来：生成中、要去配置、生成失败、有内容。
 *
 * **思维链摆在最上面**（Ant Design X 的 <Think>）：生成中是逐字长出来的实时
 * 思维链，生成完 / 重新打开时是折叠着的历史思维链。模型这轮没吐思维链
 * （reasoning 为空串）时整块不渲染 —— 摆一个空壳只会让人以为坏了。
 *
 * **失败要带动作**：没配 key 时只弹「生成失败」等于把人堵死，所以 config 类
 * 失败给「去配置」直达设置弹窗（保存成功后页面会自动重试一次，形成闭环）。
 */

import { Alert, Button, Flex, Modal, Space, Tabs, Typography } from 'antd'
import { Think } from '@ant-design/x'
import { CopyOutlined, RobotOutlined } from '@ant-design/icons'

import { noteDisplayTitle, PLATFORM_META } from '../../types/crawler'
import type { CrawlPlatform, NotePlatformCopy } from '../../types/crawler'
import { formatDateTime } from '../../utils/format'
import type { UseAiCopyResult } from './useAiCopy'

const { Text } = Typography

/** 后端只会给这两个平台的文案（固定契约，见 crawl_note_copy.py 的输出契约） */
type AiCopyPlatform = 'xhs' | 'dy'

/** 要发文案的两个平台；顺序 = 弹窗里的 Tab 顺序 */
const TARGET_PLATFORMS: AiCopyPlatform[] = ['xhs', 'dy']

interface AiCopyModalProps {
  /** useAiCopy() 的返回值，原样传进来 */
  state: UseAiCopyResult
  /** 复制一段文本（沿用页面的剪贴板 helper，全仓库只有那一套写法） */
  copyText: (text: string) => void
  /** 点「去配置」：打开 AI 配置弹窗 */
  onGoSettings: () => void
}

export default function AiCopyModal({ state, copyText, onGoSettings }: AiCopyModalProps) {
  const { target, copy, thinking, generating, error, errorKind } = state

  /** 弹窗底栏：左边是这份文案的来路（模型 / 更新时间），右边是动作 */
  const footer =
    target === null ? null : (
      <Flex justify="space-between" align="center" gap={8}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {copy ? `模型 ${copy.model} · 生成于 ${formatDateTime(copy.updated_at)}` : ''}
        </Text>
        <Space>
          <Button onClick={state.close}>关闭</Button>
          <Button type="primary" loading={generating} disabled={generating} onClick={state.regenerate}>
            换一批
          </Button>
        </Space>
      </Flex>
    )

  return (
    <Modal
      open={target !== null}
      title={
        target !== null && (
          <Space size={8}>
            <RobotOutlined />
            <span>AI 文案</span>
            <Text type="secondary" style={{ fontWeight: 'normal', fontSize: 12 }}>
              {noteDisplayTitle(target.note)}
            </Text>
          </Space>
        )
      }
      footer={footer}
      width={900}
      onCancel={state.close}
      destroyOnHidden
    >
      {/* 思维链。生成中即使还一个字没到也要摆出来（loading 态就是「在读了」），
          生成完则只在真有内容时渲染 */}
      {(generating || thinking !== '') && (
        <Think
          title={generating ? 'AI 正在思考…' : 'AI 思考过程'}
          loading={generating}
          blink={generating}
          // 生成中强制展开（思维链要边生成边看）；生成完交回组件自己管：
          // 默认收起，别把下面的文案挤下去，用户想看再点一下展开
          expanded={generating ? true : undefined}
          defaultExpanded={false}
          style={{ marginBottom: 16 }}
        >
          <Text type="secondary" style={{ whiteSpace: 'pre-wrap' }}>
            {thinking || '正在阅读这条笔记…'}
          </Text>
        </Think>
      )}

      {!generating && error !== '' && errorKind === 'config' && (
        <Alert
          type="warning"
          showIcon
          message="AI 还没配置好"
          description={<Text type="secondary">{error}</Text>}
          action={
            <Button size="small" type="primary" onClick={onGoSettings}>
              去配置
            </Button>
          }
        />
      )}

      {!generating && error !== '' && errorKind === 'generate' && (
        <Alert
          type="error"
          showIcon
          message="生成失败"
          description={<Text type="secondary">{error}</Text>}
          action={
            <Button size="small" onClick={state.regenerate}>
              重试
            </Button>
          }
        />
      )}

      {!generating && error === '' && copy !== null && (
        <Tabs
          items={TARGET_PLATFORMS.map((platform) => ({
            key: platform,
            label: PLATFORM_META[platform].label,
            children: (
              <PlatformPanel
                platform={platform}
                data={copy.platforms[platform]}
                copyText={copyText}
              />
            ),
          }))}
        />
      )}
    </Modal>
  )
}

/** 一个平台的面板：候选标题一组、候选简介一组 */
function PlatformPanel({
  platform,
  data,
  copyText,
}: {
  platform: CrawlPlatform
  data: NotePlatformCopy
  copyText: (text: string) => void
}) {
  const label = PLATFORM_META[platform].label
  return (
    <Flex vertical gap={16}>
      <CopySection title={`候选标题（${data.titles.length}）`} items={data.titles} copyText={copyText} />
      {data.intros.length > 0 ? (
        <CopySection title={`候选简介（${data.intros.length}）`} items={data.intros} copyText={copyText} />
      ) : (
        <Text type="secondary">这一轮没给出{label}简介，可以点「换一批」试试。</Text>
      )}
    </Flex>
  )
}

/** 一组可逐条复制的文案 */
function CopySection({
  title,
  items,
  copyText,
}: {
  title: string
  items: string[]
  copyText: (text: string) => void
}) {
  return (
    <Flex vertical gap={8}>
      <Text strong>{title}</Text>
      {items.map((item, index) => (
        <Flex key={`${index}-${item}`} align="flex-start" gap={8}>
          <Text type="secondary" style={{ fontSize: 12, width: 16, flexShrink: 0 }}>
            {index + 1}
          </Text>
          {/* 文案可能带换行（简介里的分点），原样保留 */}
          <Text style={{ flex: 1, whiteSpace: 'pre-wrap' }}>{item}</Text>
          <Button
            type="text"
            icon={<CopyOutlined />}
            title="复制这一条"
            onClick={() => copyText(item)}
          />
        </Flex>
      ))}
    </Flex>
  )
}
