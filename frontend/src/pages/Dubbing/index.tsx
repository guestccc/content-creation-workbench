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

import {
  CloudDownloadOutlined,
  CopyOutlined,
  DisconnectOutlined,
  PoweroffOutlined,
  ReloadOutlined,
  SoundOutlined,
} from '@ant-design/icons'
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
  modelKey,
} from '../../types/voicebox'
import type { DubbingAudio, DubbingModel, VoiceboxEnvironment } from '../../types/voicebox'
import { formatBytes, formatDateTime, formatDuration, formatElapsed } from '../../utils/format'
import DubbingBaseUrlModal from './DubbingBaseUrlModal'
import { useDubbing } from './useDubbing'
import { useDubbingEnv } from './useDubbingEnv'
import { useDubbingModels } from './useDubbingModels'

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
  // 存的是模型键（engine:model_size）而不是整个模型对象：刷新后列表会重建，
  // 存对象会拿到一份过期的副本，存键则始终指向最新的那一项
  const [chosenModelKey, setChosenModelKey] = useState('')

  const {
    env,
    loading: envLoading,
    refresh: refreshEnv,
    setBaseUrl,
    mirrorBusy,
    setMirror,
    clearMirror,
    restarting,
    restart,
  } = useDubbingEnv({ api: { message, fail, contextHolder } })
  const dubbing = useDubbing({ api: { message, fail, contextHolder } })
  const { profiles, audios, generation } = dubbing
  const {
    models,
    loading: modelsLoading,
    reload: reloadModels,
    defaultKey: defaultModelKey,
    find: findModel,
  } = useDubbingModels()

  // 选了就用选的，没选就用第一个音色（派生而不是存 state，省掉一个同步 effect）
  const effectiveProfileId = profileId || profiles[0]?.id || ''
  const effectiveProfileName =
    profiles.find((item) => item.id === effectiveProfileId)?.name ?? ''

  // 同理派生：选了就用选的，没选就用默认那个；**选中的键对不上列表时也退回默认**
  // （「重新检测」后模型列表可能变了，比如在 Voicebox 里删掉了某个模型）
  const selectedModel = findModel(chosenModelKey) ?? findModel(defaultModelKey)
  // 本次生成用的是哪个模型 —— 拿记录里的 engine/model_size 反查展示名，
  // 查不到（比如后端重启后模型列表变了）就退回原样显示参数
  const generationModel = generation
    ? findModel(modelKey({ engine: generation.engine, model_size: generation.model_size }))
    : undefined
  const generationModelText = generation
    ? (generationModel?.label ?? [generation.engine, generation.model_size].filter(Boolean).join(' '))
    : ''

  const copyText = (value: string) => {
    void navigator.clipboard.writeText(value).then(
      () => message.success('已复制'),
      () => message.error('复制失败，请手动选择复制'),
    )
  }

  /** 重新检测：环境、音色、模型一起刷（音色 / 模型刚在 Voicebox 里建好或下好的话，
   *  这一下就出来了） */
  const redetect = () => {
    void refreshEnv(true)
    void dubbing.reloadProfiles()
    void reloadModels()
  }

  const canSubmit =
    Boolean(env?.ready) &&
    !dubbing.running &&
    !dubbing.submitting &&
    effectiveProfileId !== '' &&
    selectedModel !== undefined &&
    text.trim().length > 0

  /** 提交生成 */
  const start = async () => {
    if (!selectedModel) return
    const created = await dubbing.submit({
      text: text.trim(),
      profile_id: effectiveProfileId,
      profile_name: effectiveProfileName,
      ...(filename.trim() ? { filename: filename.trim() } : {}),
      language,
      engine: selectedModel.engine,
      model_size: selectedModel.model_size,
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
      {/* 重启期间 Voicebox 是**故意**关着的：这时候报「未检测到服务」，用户会以为
          重启失败。所以那条 Alert 在 restarting 时让位给下面这条。 */}
      {restarting && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="Voicebox 正在启动"
          description={
            <Text type="secondary" style={{ fontSize: 12 }}>
              服务冷启动一般要 30 秒左右，本页会自动刷新，不用管它。
            </Text>
          }
        />
      )}

      {env && !env.ready && !restarting && (
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
                {/* 下载源是「模型下不动」的解法，而它改完必须重启才生效 ——
                    所以这两个按钮挨着放。一个按钮管两个方向：已经指向推荐镜像时
                    它变成「清除镜像」（设过的东西得能撤）。
                    hf_mirror_supported 由后端算：系统不支持、或 Voicebox 在远程
                    机器上时为 false，这时按钮点了也没用，直接不显示。 */}
                {env.hf_mirror_supported && (
                  <Button
                    size="small"
                    icon={
                      env.hf_mirror_is_recommended ? <DisconnectOutlined /> : <CloudDownloadOutlined />
                    }
                    loading={mirrorBusy}
                    onClick={() => void (env.hf_mirror_is_recommended ? clearMirror() : setMirror())}
                  >
                    {env.hf_mirror_is_recommended ? '清除镜像' : '设置镜像'}
                  </Button>
                )}
                {/* 不加 reachable 门槛：连不上时它等于「启动 Voicebox」，同样有用 */}
                {env.restart_supported && (
                  <Button
                    size="small"
                    icon={<PoweroffOutlined />}
                    loading={restarting}
                    onClick={() => void restart()}
                  >
                    重启 Voicebox
                  </Button>
                )}
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
              {/* 就绪之后上面那条 Alert 就消失了，这个按钮得在这里有一份，**两个方向
                  都要有**：少了「设置」那一半，「选了个还没下载的模型、怕下不动」这个
                  处境就没救了 —— 而那恰恰是已经就绪的时候（服务连得上、别的模型下过），
                  未就绪那条 Alert 根本不会出现。给用户设过的东西也得有地方撤销。
                  标签与未就绪那条 Alert 保持**逐字一致**：下面模型那行提示里引号中的
                  按钮名，得能在页面上真的找到。 */}
              {env.hf_mirror_supported && (
                // 不加图标：这一行另外两个（「更改服务地址」「重新检测」）都是纯文字
                // 链接按钮，只有它带图标会显得像是另一类操作
                <Button
                  size="small"
                  type="link"
                  style={{ padding: 0, fontSize: 12 }}
                  loading={mirrorBusy}
                  onClick={() => void (env.hf_mirror_is_recommended ? clearMirror() : setMirror())}
                >
                  {env.hf_mirror_is_recommended ? '清除镜像' : '设置镜像'}
                </Button>
              )}
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
                <Text strong>模型</Text>
              </div>
              <Select
                style={{ width: '100%' }}
                placeholder={models.length === 0 ? '读不到模型列表' : '选择一个模型'}
                value={selectedModel ? modelKey(selectedModel) : undefined}
                loading={modelsLoading}
                onChange={setChosenModelKey}
                // 下载状态直接写进选项里：选一个没下好的模型要多等几 GB 下载，
                // 这件事得在**选之前**看得见，不是选完再告诉用户
                options={models.map((item) => ({
                  value: modelKey(item),
                  label: `${item.label} · ${modelStatusLabel(item)}`,
                  title: item.note,
                }))}
                notFoundContent={
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    读不到模型：确认 Voicebox 已打开，然后点上面的「重新检测」
                  </Text>
                }
              />
            </div>
          </Flex>

          {selectedModel && (
            <Text
              type={selectedModel.downloaded === false ? 'warning' : 'secondary'}
              style={{ fontSize: 12 }}
            >
              {modelHint(selectedModel, env)}
            </Text>
          )}

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
              {/* 模型也要说：慢的时候用户第一个想确认的就是「现在跑的是哪个」 */}
              {generationModelText && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  模型：{generationModelText}
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

/**
 * 模型下拉底下那行说明。
 *
 * 「没下载」这句里要不要带「点设置镜像」，**得看那个按钮此刻在不在页面上**：
 * - 服务指向远程机器、或系统不支持时，后端给的 hf_mirror_supported 是 false，
 *   按钮不渲染 —— 这时候再让用户去点，就是让他去找一个不存在的东西；
 * - 镜像**已经**指向推荐源时按钮写的是「清除镜像」，同理不能再劝他「设置」。
 *   （原来的写法没管这两条，在「已就绪 + 已设镜像」下指向了一个页面上没有的按钮。）
 *
 * 其余情况只如实说清要等多久 —— 这句提示的首要任务是让用户**在点生成之前**知道
 * 会等一个几 GB 的下载，而不是推销镜像。
 */
function modelHint(model: DubbingModel, env: VoiceboxEnvironment | null): string {
  if (model.downloaded !== false) return model.note
  const mirrorTip =
    env?.hf_mirror_supported && !env.hf_mirror_is_recommended
      ? '；下不动就在上面点「设置镜像」'
      : ''
  return `${model.note} · 这个模型还没下载，第一次生成会先花时间下载${mirrorTip}`
}

/**
 * 模型当前可用到什么程度 → 下拉里那句短标注。
 *
 * 「使用中」说的是 Voicebox **此刻已加载进显存**的那个（马上就能跑）；「已下载」
 * 是下好了但没加载（选它要等一次加载）。「状态未知」不能并进「未下载」——
 * 那是上游列表里没有这个名字（多半是 Voicebox 改过版本），用户什么都不用做，
 * 更不该被引导去点下载。
 */
function modelStatusLabel(model: DubbingModel): string {
  if (model.loaded) return '使用中'
  if (model.downloaded === true) return '已下载'
  if (model.downloaded === false) return '未下载'
  return '状态未知'
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
