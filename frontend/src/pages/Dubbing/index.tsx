/**
 * 智能配音页面。
 *
 * 流程：确认 Voicebox 服务在（顶部 Alert）→ 选音色 + 贴文案 → 生成 → 看进度 →
 * 在下面的产物清单里试听/下载/删除。
 *
 * 与其它页面的一处结构差异：**没有历史任务列表**。后端对 Voicebox 只做 HTTP
 * 代理，生成记录活在进程内存里，产物落在 materials/dubbing/ —— 所以「历史」
 * 就是这份扫盘得到的产物清单，刷新页面、重启后端都不会丢。
 *
 * 音色的克隆/试听/调参全在 Voicebox 自己的界面里做，这里只从它的音色列表里
 * 挑一个用；同理，模型下载、显卡设置都不在本页面。
 *
 * 页面自己不写状态机：环境探测与指定地址在本文件夹的 useDubbingEnv，
 * 音色/提交/轮询/产物清单在 useDubbing（都是单页私有，不进公共 hooks）。
 */

import { CopyOutlined, ReloadOutlined, SoundOutlined } from '@ant-design/icons'
import { useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Empty,
  Flex,
  Input,
  Popconfirm,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd'

import { useApiMessage } from '../../hooks'
import {
  GENERATION_STATUS_META,
  LANGUAGE_OPTIONS,
  MAX_TEXT_CHARS,
  MODEL_SIZE_OPTIONS,
} from '../../types/voicebox'
import type { DubbingAudio } from '../../types/voicebox'
import { formatBytes, formatDateTime, formatDuration, formatElapsed } from '../../utils/format'
import DubbingBaseUrlModal from './DubbingBaseUrlModal'
import { useDubbing } from './useDubbing'
import { useDubbingEnv } from './useDubbingEnv'

const { Text, Title, Paragraph } = Typography

/** 地址来源的展示标签 */
const SOURCE_LABEL: Record<string, string> = {
  environment: '系统环境变量',
  env_file: '.env 指定',
  default: '默认地址',
}

export default function Dubbing() {
  const { message, fail, contextHolder } = useApiMessage()
  const [baseUrlOpen, setBaseUrlOpen] = useState(false)

  // ---------- 表单 ----------
  const [profileId, setProfileId] = useState('')
  const [text, setText] = useState('')
  const [filename, setFilename] = useState('')
  const [language, setLanguage] = useState('zh')
  const [modelSize, setModelSize] = useState('1.7B')

  const { env, loading: envLoading, refresh: refreshEnv, setBaseUrl } = useDubbingEnv({
    api: { message, fail, contextHolder },
  })
  const dubbing = useDubbing({ api: { message, fail, contextHolder } })
  const { profiles, audios, generation } = dubbing

  // 选了就用选的，没选就用第一个音色（派生而不是存 state，省掉一个同步 effect）
  const effectiveProfileId = profileId || profiles[0]?.id || ''
  const effectiveProfileName =
    profiles.find((item) => item.id === effectiveProfileId)?.name ?? ''

  const copyText = (value: string) => {
    void navigator.clipboard.writeText(value).then(
      () => message.success('已复制'),
      () => message.error('复制失败，请手动选择复制'),
    )
  }

  /** 重新检测：环境与音色一起刷（音色是刚在 Voicebox 里建的话，这一下就出来了） */
  const redetect = () => {
    void refreshEnv(true)
    void dubbing.reloadProfiles()
  }

  const canSubmit =
    Boolean(env?.ready) &&
    !dubbing.running &&
    !dubbing.submitting &&
    effectiveProfileId !== '' &&
    text.trim().length > 0

  /** 提交生成 */
  const start = async () => {
    const created = await dubbing.submit({
      text: text.trim(),
      profile_id: effectiveProfileId,
      profile_name: effectiveProfileName,
      ...(filename.trim() ? { filename: filename.trim() } : {}),
      language,
      model_size: modelSize,
    })
    if (created) {
      message.success('已开始生成配音，下面可以看进度')
    }
  }

  const statusMeta = generation ? GENERATION_STATUS_META[generation.status] : null

  return (
    <div className="page">
      {contextHolder}

      <div className="page__header">
        <Title level={3} style={{ marginBottom: 4 }}>
          <SoundOutlined style={{ marginRight: 8 }} />
          智能配音
        </Title>
        <Paragraph type="secondary" style={{ marginBottom: 0 }}>
          用本机的 Voicebox 把文案念成配音。音色在 Voicebox 里建好，这里选一个用，
          产物统一落到 materials/dubbing/。
        </Paragraph>
      </div>

      {/* ---------- 环境自检 ---------- */}
      {env && !env.ready && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={env.reachable ? 'Voicebox 模型还没就绪' : '未检测到 Voicebox 服务'}
          description={
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              {env.detail && <Text>{env.detail}</Text>}
              {env.fix_hint && <Text strong>{env.fix_hint}</Text>}
              {env.install_hints.map((hint, index) => (
                <div key={index}>
                  <Space size={8}>
                    <Text strong>{hint.title}</Text>
                    {hint.command && (
                      <>
                        <Text code style={{ fontSize: 12 }}>
                          {hint.command}
                        </Text>
                        <Button
                          size="small"
                          type="text"
                          icon={<CopyOutlined />}
                          onClick={() => copyText(hint.command)}
                        />
                      </>
                    )}
                  </Space>
                  {hint.note && (
                    <div>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {hint.note}
                      </Text>
                    </div>
                  )}
                  {hint.url && (
                    <div>
                      <a href={hint.url} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
                        {hint.url}
                      </a>
                    </div>
                  )}
                </div>
              ))}
              <Space size={8} wrap>
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={envLoading}
                  onClick={redetect}
                >
                  重新检测
                </Button>
                <Button size="small" onClick={() => setBaseUrlOpen(true)}>
                  指定服务地址
                </Button>
              </Space>
            </Space>
          }
        />
      )}

      {env?.ready && (
        <Alert
          type="success"
          showIcon
          style={{ marginBottom: 16 }}
          message={
            <Space size={8} wrap>
              <span>Voicebox 就绪</span>
              {env.model_size && <Tag color="green">模型 {env.model_size}</Tag>}
              <Tag color={env.gpu_available ? 'blue' : 'default'}>
                {env.gpu_available ? 'GPU 加速' : 'CPU（较慢）'}
              </Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>
                音色 {env.profile_count} 个
              </Text>
            </Space>
          }
          description={
            <Space size={8} wrap>
              <Text type="secondary" style={{ fontSize: 12 }}>
                服务：{env.base_url}（{SOURCE_LABEL[env.base_url_source] ?? env.base_url_source}）
                · 产物目录：{env.default_output_dir}
              </Text>
              <Button
                size="small"
                type="link"
                style={{ padding: 0, fontSize: 12 }}
                onClick={() => setBaseUrlOpen(true)}
              >
                更改服务地址
              </Button>
              <Button
                size="small"
                type="link"
                style={{ padding: 0, fontSize: 12 }}
                loading={envLoading}
                onClick={redetect}
              >
                重新检测
              </Button>
            </Space>
          }
        />
      )}

      {env?.warnings.map((warning, index) => (
        <Alert
          key={index}
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={warning}
        />
      ))}

      {/* ---------- 生成 ---------- */}
      <Card title="生成配音" style={{ marginBottom: 16 }}>
        <Flex vertical gap={12}>
          <Flex gap={12} wrap>
            <div style={{ flex: '1 1 260px', minWidth: 220 }}>
              <div style={{ marginBottom: 4 }}>
                <Text strong>音色</Text>
              </div>
              <Select
                style={{ width: '100%' }}
                placeholder={profiles.length === 0 ? '还没有音色' : '选择一个音色'}
                value={effectiveProfileId || undefined}
                loading={dubbing.profilesLoading}
                onChange={setProfileId}
                options={profiles.map((item) => ({
                  value: item.id,
                  label: item.description ? `${item.name}（${item.description}）` : item.name,
                }))}
                notFoundContent={
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    读不到音色：确认 Voicebox 已打开，并在它的「Profiles」里至少建一个音色
                  </Text>
                }
              />
            </div>
            <div style={{ flex: '0 0 140px' }}>
              <div style={{ marginBottom: 4 }}>
                <Text strong>语言</Text>
              </div>
              <Select
                style={{ width: '100%' }}
                value={language}
                onChange={setLanguage}
                options={[...LANGUAGE_OPTIONS]}
              />
            </div>
            <div style={{ flex: '1 1 200px' }}>
              <div style={{ marginBottom: 4 }}>
                <Text strong>模型规模</Text>
              </div>
              <Select
                style={{ width: '100%' }}
                value={modelSize}
                onChange={setModelSize}
                options={[...MODEL_SIZE_OPTIONS]}
              />
            </div>
          </Flex>

          <div>
            <div style={{ marginBottom: 4 }}>
              <Text strong>配音文案</Text>
            </div>
            <Input.TextArea
              value={text}
              onChange={(event) => setText(event.target.value)}
              maxLength={MAX_TEXT_CHARS}
              showCount
              autoSize={{ minRows: 4, maxRows: 10 }}
              placeholder="粘贴要念的文案，最多 5000 字"
            />
          </div>

          <Flex gap={12} wrap align="flex-end">
            <div style={{ flex: '1 1 260px', minWidth: 220 }}>
              <div style={{ marginBottom: 4 }}>
                <Text strong>产物名</Text>
              </div>
              <Input
                value={filename}
                onChange={(event) => setFilename(event.target.value)}
                placeholder="留空自动命名（dub_时间戳）"
                allowClear
              />
            </div>
            <Button
              type="primary"
              icon={<SoundOutlined />}
              disabled={!canSubmit}
              loading={dubbing.submitting}
              onClick={() => void start()}
            >
              生成配音
            </Button>
          </Flex>

          {!env?.ready && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              服务未就绪时不能生成：按上面那条提示把 Voicebox 打开（或指定服务地址）。
            </Text>
          )}
        </Flex>
      </Card>

      {/* ---------- 本次生成的进度 ---------- */}
      {generation && statusMeta && (
        <Alert
          type={generation.status === 'failed' ? 'error' : generation.status === 'success' ? 'success' : 'info'}
          showIcon
          style={{ marginBottom: 16 }}
          message={
            <Space size={8} wrap>
              <Tag color={statusMeta.color}>{statusMeta.label}</Tag>
              <Text>{generation.progress_hint || statusMeta.hint}</Text>
              {!isFinished(generation.status) && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  已耗时 {formatElapsed(generation.elapsed_seconds)}
                </Text>
              )}
            </Space>
          }
          description={
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                文案：{generation.text_excerpt}
              </Text>
              {generation.profile_name && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  音色：{generation.profile_name}
                </Text>
              )}
              {generation.status === 'failed' && generation.error_message && (
                <Text type="danger">{generation.error_message}</Text>
              )}
              {generation.status === 'running' && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  生成在 Voicebox 里跑，这个页面可以关掉；产物会出现在下面的清单里。
                </Text>
              )}
            </Space>
          }
        />
      )}

      {/* ---------- 产物清单 ---------- */}
      <Card
        title={`配音产物（${audios.length}）`}
        extra={
          <Button size="small" loading={dubbing.audiosLoading} onClick={() => void dubbing.reloadAudios()}>
            刷新
          </Button>
        }
      >
        <Spin spinning={dubbing.audiosLoading}>
          {audios.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="还没有配音产物，先在上面生成一条"
            />
          ) : (
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
                gap: 12,
              }}
            >
              {audios.map((audio) => (
                <AudioCard
                  key={audio.name}
                  audio={audio}
                  onRemove={() => void dubbing.removeAudio(audio.name)}
                />
              ))}
            </div>
          )}
        </Spin>
        {env?.default_output_dir && (
          <Paragraph type="secondary" style={{ fontSize: 12, margin: '12px 0 0' }}>
            产物目录：<Text code>{env.default_output_dir}</Text>（删掉的是磁盘上的文件本身）
          </Paragraph>
        )}
      </Card>

      <DubbingBaseUrlModal
        open={baseUrlOpen}
        env={env}
        onSave={setBaseUrl}
        onClose={() => setBaseUrlOpen(false)}
      />
    </div>
  )
}

/** 生成是否已经结束（结束就不再显示「已耗时」） */
function isFinished(status: string): boolean {
  return status === 'success' || status === 'failed'
}

/** 一张产物卡片：试听 + 元信息 + 删除 */
function AudioCard({ audio, onRemove }: { audio: DubbingAudio; onRemove: () => void }) {
  return (
    <Card styles={{ body: { padding: 12 } }}>
      <Flex vertical gap={8}>
        <Flex align="center" gap={8} wrap>
          <Text strong ellipsis={{ tooltip: audio.name }} style={{ maxWidth: 200 }}>
            {audio.name}
          </Text>
          {audio.profile_name && <Tag>{audio.profile_name}</Tag>}
          {!audio.indexed && (
            <Tag color="default" title="不是本功能生成的，索引里没有它的音色/文案">
              外部文件
            </Tag>
          )}
        </Flex>

        {/* 项目里没有音频组件，原生 <audio> 最省事：能试听、能拖动进度条 */}
        <audio controls preload="none" src={audio.audio_url} style={{ width: '100%' }} />

        <Flex align="center" gap={8} wrap>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {formatDuration(audio.duration)} · {formatBytes(audio.size_bytes)}
            {audio.created_at ? ` · ${formatDateTime(audio.created_at)}` : ''}
          </Text>
          <Popconfirm
            title="删除这份配音？"
            description={`会从产物目录里删掉 ${audio.name}，不可恢复。`}
            okText="删除"
            cancelText="取消"
            onConfirm={onRemove}
          >
            <Button size="small" type="text" danger style={{ marginLeft: 'auto' }}>
              删除
            </Button>
          </Popconfirm>
        </Flex>

        {audio.text_excerpt && (
          <Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: audio.text_excerpt }}>
            文案：{audio.text_excerpt}
          </Text>
        )}
      </Flex>
    </Card>
  )
}
