/**
 * 智能镜头分割页面。
 *
 * 流程：选素材目录 → 选模板 → 预览切点 / 直接切分 → 看进度。
 * 切出的片段不在页面上铺开（一堆视频会把页面拉得很长），要看的去
 * 任务里下钻：历史任务或进度卡里的「查看」→ 某条视频的片段。
 *
 * 两个关键设计：
 * 1. 任务由后端异步执行，页面用轮询拿进度（前端 fetch 超时 15 秒，
 *    而切一条两分钟素材就要几分钟，同步接口必然超时）；
 * 2. 预览与切分是两个独立任务（preview 不写用户目录，只把切点存库）。
 *
 * 页面自己不写状态机：目录扫描、任务生命周期、历史列表分别在 useSourceDir /
 * useJobRunner / useJobList 里，这里只做编排与布局。
 */

import {
  BlockOutlined,
  EyeOutlined,
  PlayCircleOutlined,
  ScissorOutlined,
} from '@ant-design/icons'
import { useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Empty,
  Flex,
  Image,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Radio,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType, TableProps } from 'antd/es/table'

import DirectoryPicker from '../components/DirectoryPicker'
import HistoryCard from '../components/HistoryCard'
import JobProgressCard, { JobTitle } from '../components/JobProgressCard'
import SourceDirCard from '../components/SourceDirCard'
import VideoPreviewModal from '../components/VideoPreviewModal'
import {
  jobActionsColumn,
  jobCreatedColumn,
  jobIdColumn,
  jobInputColumn,
  jobStatusColumn,
} from '../components/jobColumns'
import {
  useApiMessage,
  useAsyncData,
  useDirectoryPicker,
  useJobList,
  useJobPolling,
  useJobRunner,
  usePurgeFiles,
  useSourceDir,
} from '../hooks'
import type { UsePurgeFilesResult } from '../hooks'
import {
  cancelSceneJob,
  createSceneJob,
  batchDeleteSceneJobs,
  deleteSceneJob,
  fetchSceneClips,
  fetchSceneEnvironment,
  fetchSceneJob,
  fetchSceneJobs,
  fetchSceneSummary,
  fetchSceneTemplates,
  retrySceneItem,
} from '../api/scene'
import {
  DETECTOR_OPTIONS,
  ITEM_STATUS_META,
  JOB_STATUS_META,
  isTerminalStatus,
} from '../types/scene'
import type {
  SceneClip,
  SceneJob,
  SceneJobItem,
  SceneJobMode,
  SceneJobPayload,
  SceneSummary,
} from '../types/scene'
import { formatBytes, formatDuration } from '../utils/format'

const { Text, Title, Paragraph } = Typography

/** 片段封面上的播放角标：常驻的半透明三角，提示这一片是可以点的（卡片窄，图标跟着缩小） */
const PLAY_BADGE: CSSProperties = {
  position: 'absolute',
  inset: 0,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontSize: 16,
  color: 'rgba(255, 255, 255, 0.85)',
  // 图标是 svg，textShadow 对它不生效，改用 filter 描一层暗边把底下的画面压住
  filter: 'drop-shadow(0 1px 6px rgba(0, 0, 0, 0.8))',
  pointerEvents: 'none',
}

/**
 * 播放器的状态：正在放的那一片，以及它所属的那一组片段。
 *
 * 连列表一起记住，是为了让「上一个 / 下一个」知道该在哪儿走 —— 从某条
 * 视频的片段弹窗里点开，范围就是那条视频自己的片段，不会串到别人家去。
 */
interface PlayerState {
  clips: SceneClip[]
  index: number
}

/** 自定义模板的默认参数 */
const DEFAULT_CUSTOM = {
  detector: 'adaptive',
  /** null 表示用检测器默认阈值 */
  threshold: null as number | null,
  minLen: 0.6,
  copy: false,
}

export default function SceneSplit() {
  const { message, fail, contextHolder } = useApiMessage()
  const dir = useSourceDir(fail)
  const picker = useDirectoryPicker<'input' | 'output'>()

  // ---------- 输出与模板 ----------
  const [outputDir, setOutputDir] = useState('')
  const [templateKey, setTemplateKey] = useState('standard')
  const [custom, setCustom] = useState(DEFAULT_CUSTOM)
  /** 自定义模式下是否使用检测器默认阈值 */
  const [useDefaultThreshold, setUseDefaultThreshold] = useState(true)

  // ---------- 结果 ----------
  const [summary, setSummary] = useState<SceneSummary | null>(null)
  /** 播放器；null 表示关着 */
  const [player, setPlayer] = useState<PlayerState | null>(null)

  // ---------- 历史任务的下钻弹窗 ----------
  // 两层：先看这条任务切了哪些视频，再看某一条视频切出的片段。
  /** 第一层弹窗看的是哪条任务；null 表示关着 */
  const [detailJobId, setDetailJobId] = useState<number | null>(null)
  const [detailJob, setDetailJob] = useState<SceneJob | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  /** 第二层弹窗看的是哪条视频 */
  const [detailItem, setDetailItem] = useState<SceneJobItem | null>(null)
  /** 该视频的片段；预览任务没有片段文件，这里保持空数组 */
  const [detailItemClips, setDetailItemClips] = useState<SceneClip[]>([])
  const [detailItemLoading, setDetailItemLoading] = useState(false)

  /** 预览任务结束时拉切点汇总；切分结果不在页面铺开，用户去任务里查看 */
  const loadSummary = async (target: SceneJob) => {
    if (target.mode !== 'preview') {
      return
    }
    try {
      setSummary(await fetchSceneSummary(target.id))
    } catch {
      // 结果拉取失败不打扰用户，界面上会显示为空
    }
  }

  /** 关掉第一层弹窗：顺手把第二层也关掉，别留个孤儿挂在后面 */
  const closeJobDetail = () => {
    setDetailJobId(null)
    setDetailJob(null)
    setDetailItem(null)
  }

  const history = useJobList<SceneJob>({ fetchList: fetchSceneJobs })

  // 删除时是否连产物一起清（每个删除确认框里都有这个勾选项）
  const purge = usePurgeFiles()

  const runner = useJobRunner<SceneJob, SceneJobPayload>({
    create: createSceneJob,
    cancel: cancelSceneJob,
    // take() 在这里调用：删除那一刻取值并重置，勾选只对这一次删除有效
    remove: (jobId) => deleteSceneJob(jobId, purge.take()),
    batchRemove: (ids) => batchDeleteSceneJobs(ids, purge.take()),
    fetchJob: fetchSceneJob,
    isTerminal: (job) => isTerminalStatus(job.status),
    fail,
    onChanged: history.reload,
    onFinished: (job) => void loadSummary(job),
    onRemoved: (jobId, wasCurrent) => {
      if (wasCurrent) {
        setSummary(null)
      }
      if (detailJobId === jobId) {
        // 正在弹窗里看这条：记录没了就别让它挂在那儿
        closeJobDetail()
      }
    },
  })

  // 弹窗里那条任务还在跑时同样轮询：清单上的进度才不是打开那一刻的快照。
  // 轮询由 useJobPolling 自己按「哪条任务、是否在跑」判断，终态一到就停。
  useJobPolling({
    job: detailJob,
    fetchJob: fetchSceneJob,
    isTerminal: (job) => isTerminalStatus(job.status),
    onUpdate: setDetailJob,
  })

  // 进页面：环境自检 + 模板，并把输入/输出默认停在素材目录的两个分段
  // （materials/source → materials/clips）。函数式更新 + 空值判断，避免覆盖
  // 用户在这两个请求返回前已经手动选好的目录。
  const bootstrap = useAsyncData({
    load: async () => {
      const [environment, templates] = await Promise.all([
        fetchSceneEnvironment(),
        fetchSceneTemplates(),
      ])
      return { environment, templates }
    },
    failMessage: '初始化失败',
    fail,
    onLoaded: ({ environment }) => {
      if (environment.default_input_dir) {
        dir.setPath((current) => current || environment.default_input_dir)
      }
      if (environment.default_output_dir) {
        setOutputDir((current) => current || environment.default_output_dir)
      }
    },
  })

  const environment = bootstrap.data?.environment ?? null
  const templates = bootstrap.data?.templates ?? []
  const selectedTemplate = templates.find((item) => item.key === templateKey)

  /** 当前看的是不是素材目录的 source 分段 —— 空列表时的提示文案要分情况 */
  const isEmptySourceDir =
    Boolean(environment?.default_input_dir) && dir.data?.path === environment?.default_input_dir

  /** 创建任务 */
  const start = async (mode: SceneJobMode) => {
    if (!dir.path) {
      message.warning('请先选择素材目录')
      return
    }
    if (dir.videos.length === 0 && dir.selected.length === 0) {
      message.warning('该目录下没有可处理的视频文件')
      return
    }
    if (mode === 'split' && !outputDir) {
      message.warning('请先选择输出目录')
      return
    }

    const created = await runner.submit({
      input_path: dir.path,
      mode,
      template: templateKey,
      recursive: dir.recursive,
      ...(dir.selected.length > 0 ? { files: dir.selected } : {}),
      ...(mode === 'split' ? { output_dir: outputDir } : {}),
      ...(templateKey === 'custom'
        ? {
            detector: custom.detector,
            threshold: useDefaultThreshold ? null : custom.threshold,
            min_len: custom.minLen,
            copy: custom.copy,
          }
        : {}),
    })
    if (!created) {
      return
    }
    setSummary(null)
    message.success(mode === 'preview' ? '已开始检测切点，请稍候' : '已开始切割，可以在下面看进度')
  }

  /** 取消任务 */
  const cancel = async (jobId: number) => {
    if (await runner.cancel(jobId)) {
      message.info('已请求取消')
    }
  }

  /**
   * 重试单条失败的视频：条目重置回 pending、任务重新入队。
   *
   * 返回的是整个任务的新状态，两处都要对上号地刷新：进度卡那条（页面的
   * 当前任务）和历史弹窗里正在看的那条 —— 对不上号的保持原样，互不干扰。
   */
  const retryItem = async (target: SceneJob, item: SceneJobItem) => {
    try {
      const updated = await retrySceneItem(target.id, item.index)
      runner.setJob((current) => (current?.id === updated.id ? updated : current))
      setDetailJob((current) => (current?.id === updated.id ? updated : current))
      history.reload()
      message.success(`已重新入队：${item.source_name}`)
    } catch (error) {
      fail(error, '重试失败')
    }
  }

  /** 删除任务记录（是否连片段文件一起删由确认框里的勾选决定） */
  const remove = async (jobId: number) => {
    if (await runner.remove(jobId)) {
      message.success('已删除任务记录')
    }
  }

  /** 批量删除历史记录（整批成功或整批失败） */
  const batchRemove = async () => {
    const ids = history.selectedRowKeys
    if (await runner.removeMany(ids)) {
      message.success(`已删除 ${ids.length} 条任务记录`)
      history.clearSelection()
    }
  }

  /** 历史表行多选：只终态任务可选（运行中的任务禁止勾选） */
  const historyRowSelection: TableProps<SceneJob>['rowSelection'] = {
    selectedRowKeys: history.selectedRowKeys,
    onChange: (keys) => history.setSelectedRowKeys(keys.map(Number)),
    getCheckboxProps: (job) => ({ disabled: !isTerminalStatus(job.status) }),
  }

  /**
   * 历史列表点「查看」：弹窗展示这条任务切了哪些视频。
   *
   * 刻意不动页面上的 `job` —— 页面上那套进度卡和结果区属于「我正在跑的
   * 那条任务」，翻旧账不该把它顶掉；弹窗关掉，页面还是原样。
   */
  const openJobDetail = async (jobId: number) => {
    setDetailJobId(jobId)
    setDetailJob(null)
    setDetailItem(null)
    setDetailLoading(true)
    const detail = await runner.read(jobId)
    setDetailLoading(false)
    if (detail) {
      setDetailJob(detail)
    } else {
      // 读不到就整个收起来，免得留一个空弹窗在那儿
      setDetailJobId(null)
    }
  }

  /**
   * 清单里点「查看」：看这一条视频自己切出的片段。
   *
   * 归属按后端给的 item_index 判，不拿文件名去猜 —— 递归扫描时两条视频
   * 同名是常事，按名字归组会把它们的片段混在一起。预览任务没有片段文件，
   * 直接展示条目里带的切点清单。
   */
  const openItemDetail = async (target: SceneJob, item: SceneJobItem) => {
    setDetailItem(item)
    setDetailItemClips([])
    if (target.mode === 'preview' || item.clip_count === 0) {
      // 没切出片段的（单镜头、失败、还没轮到）不必白跑一趟接口
      return
    }
    setDetailItemLoading(true)
    try {
      const all = await fetchSceneClips(target.id)
      setDetailItemClips(all.filter((clip) => clip.item_index === item.index))
    } catch (error) {
      fail(error, '读取片段失败')
    } finally {
      setDetailItemLoading(false)
    }
  }

  /** 打开播放器：list 是这一片所在的那一组，上一个/下一个在它里面走 */
  const playClip = (list: SceneClip[], index: number) => {
    if (index >= 0 && index < list.length) {
      setPlayer({ clips: list, index })
    }
  }

  /** 播放器里切上一个/下一个；到头就停在原地（按钮那边也已经置灰） */
  const stepPlayer = (delta: number) => {
    setPlayer((current) => {
      if (!current) {
        return current
      }
      const next = current.index + delta
      return next >= 0 && next < current.clips.length ? { ...current, index: next } : current
    })
  }

  const missingDeps = environment?.dependencies.filter((dep) => !dep.ok && dep.name !== 'ffprobe') ?? []
  /** 正在放的那一片 */
  const playingClip = player ? player.clips[player.index] : null
  const job = runner.job

  return (
    <div className="page">
      {contextHolder}

      <div className="page__header">
        <Title level={3} style={{ marginBottom: 4 }}>
          {/* 图标与文字之间靠 marginRight 留白：JSX 会把跨行的缩进吃掉，不显式留会贴在一起 */}
          <ScissorOutlined style={{ marginRight: 8 }} />
          智能镜头分割
        </Title>
        <Paragraph type="secondary" style={{ marginBottom: 0 }}>
          按画面跳变把多镜头素材切成单镜头片段。先「预览切点」看效果，确认后再「开始切分」。
        </Paragraph>
      </div>

      {/* ---------- 环境自检 ---------- */}
      {environment && !environment.ready && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="运行环境不完整，无法执行切割"
          description={
            <Space direction="vertical" size={4}>
              {missingDeps.map((dep) => (
                <Text key={dep.name}>
                  <Text strong>{dep.name}</Text>：{dep.detail}
                  {dep.fix_hint && (
                    <>
                      {' '}
                      → <Text code>{dep.fix_hint}</Text>
                    </>
                  )}
                </Text>
              ))}
            </Space>
          }
        />
      )}

      {/* ---------- 输入与模板 ---------- */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} lg={14}>
          <SourceDirCard
            dir={dir}
            outputDir={outputDir}
            outputLabel="输出目录（切出的片段放这里）"
            outputPlaceholder="尚未选择"
            outputHint="每条视频的片段会放进独立的子目录（视频名_scenes），不会混在一起"
            isEmptySourceDir={isEmptySourceDir}
            onPickInput={() => picker.open('input')}
            onPickOutput={() => picker.open('output')}
          />
        </Col>

        <Col xs={24} lg={10}>
          <Card
            title={
              <Space size={8}>
                <BlockOutlined style={{ color: 'var(--color-primary)' }} />
                选择切割模板
              </Space>
            }
            style={{ height: '100%' }}
          >
            <Radio.Group
              value={templateKey}
              onChange={(event) => setTemplateKey(event.target.value as string)}
              style={{ width: '100%' }}
            >
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                {templates.map((template) => (
                  <Card
                    key={template.key}
                    hoverable
                    onClick={() => setTemplateKey(template.key)}
                    style={{
                      borderColor:
                        templateKey === template.key ? 'var(--color-primary)' : undefined,
                      background:
                        templateKey === template.key ? 'var(--color-primary-soft)' : undefined,
                    }}
                    styles={{ body: { padding: '10px 12px' } }}
                  >
                    <Flex align="center" gap={8}>
                      <Radio value={template.key} />
                      <div style={{ flex: 1 }}>
                        <Space size={6}>
                          <Text strong>{template.name}</Text>
                          {template.recommended && <Tag color="blue">推荐</Tag>}
                        </Space>
                        <div>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {template.summary}
                          </Text>
                        </div>
                      </div>
                    </Flex>
                  </Card>
                ))}
              </Space>
            </Radio.Group>

            {selectedTemplate && selectedTemplate.key !== 'custom' && (
              <div style={{ marginTop: 12 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {selectedTemplate.best_for}
                </Text>
                <div style={{ marginTop: 6 }}>
                  <Tag>{selectedTemplate.detector_label}</Tag>
                  <Tag>阈值 {selectedTemplate.threshold_label}</Tag>
                  <Tag>最短镜头 {selectedTemplate.min_len}s</Tag>
                </div>
              </div>
            )}

            {/* 自定义参数 */}
            {templateKey === 'custom' && (
              <div style={{ marginTop: 12 }}>
                <Space direction="vertical" size={8} style={{ width: '100%' }}>
                  <div>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      检测器
                    </Text>
                    <Select
                      value={custom.detector}
                      onChange={(value) => setCustom((c) => ({ ...c, detector: value }))}
                      options={DETECTOR_OPTIONS}
                      style={{ width: '100%' }}
                    />
                  </div>
                  <Flex align="center" gap={8}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      使用检测器默认阈值
                    </Text>
                    <Switch checked={useDefaultThreshold} onChange={setUseDefaultThreshold} />
                  </Flex>
                  {!useDefaultThreshold && (
                    <Flex align="center" gap={8}>
                      <Text type="secondary" style={{ fontSize: 12, width: 72 }}>
                        阈值
                      </Text>
                      <InputNumber
                        min={0.1}
                        max={100}
                        step={0.5}
                        value={custom.threshold ?? 3}
                        onChange={(value) => setCustom((c) => ({ ...c, threshold: value ?? 3 }))}
                        style={{ flex: 1 }}
                      />
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        越小越敏感
                      </Text>
                    </Flex>
                  )}
                  <Flex align="center" gap={8}>
                    <Text type="secondary" style={{ fontSize: 12, width: 72 }}>
                      最短镜头
                    </Text>
                    <InputNumber
                      min={0.1}
                      max={30}
                      step={0.1}
                      value={custom.minLen}
                      onChange={(value) => setCustom((c) => ({ ...c, minLen: value ?? 0.6 }))}
                      addonAfter="秒"
                      style={{ flex: 1 }}
                    />
                  </Flex>
                  <Flex align="center" gap={8}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      快速复制（不重编码）
                    </Text>
                    <Switch
                      checked={custom.copy}
                      onChange={(checked) => setCustom((c) => ({ ...c, copy: checked }))}
                    />
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      快且无损，但片段起点只能落在关键帧
                    </Text>
                  </Flex>
                </Space>
              </div>
            )}
          </Card>
        </Col>
      </Row>

      {/* ---------- 动作区 ---------- */}
      <Card style={{ marginBottom: 16 }}>
        <Flex align="center" justify="space-between" wrap gap={12}>
          <Space>
            <Button
              icon={<EyeOutlined />}
              onClick={() => void start('preview')}
              loading={runner.submitting}
              disabled={!environment?.ready}
            >
              预览切点
            </Button>
            <Button
              type="primary"
              icon={<ScissorOutlined />}
              onClick={() => void start('split')}
              loading={runner.submitting}
              disabled={!environment?.ready || runner.running}
            >
              开始切分
            </Button>
            {runner.running && job && (
              <Popconfirm
                title="取消当前任务？"
                description="已切出的片段会保留，未处理的视频会被跳过。"
                okText="取消任务"
                cancelText="继续跑"
                onConfirm={() => void cancel(job.id)}
              >
                <Button danger>停止</Button>
              </Popconfirm>
            )}
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            预览只检测切点、不写文件；切分会按模板把每个镜头导出成独立片段
          </Text>
        </Flex>
      </Card>

      {/* ---------- 进度 ---------- */}
      {job && (
        <JobProgressCard
          title={
            <JobTitle
              jobId={job.id}
              meta={JOB_STATUS_META[job.status]}
              extra={<Tag>{job.mode === 'preview' ? '预览切点' : '切分片段'}</Tag>}
            />
          }
          status={job.status}
          percent={job.progress_percent}
          running={runner.running}
          stats={[
            {
              label: '视频进度',
              value: `${job.completed_videos} / ${job.total_videos} 条`,
            },
            { label: '已切出片段', value: `${job.clip_count} 个` },
            { label: '检测镜头数', value: `${job.scene_count} 个` },
            {
              label: '耗时',
              value: formatDuration(
                job.started_at ? (Date.now() - new Date(job.started_at).getTime()) / 1000 : null,
              ),
            },
          ]}
          current={
            job.current_video
              ? {
                  label: '处理',
                  index: job.current_index,
                  total: job.total_videos,
                  name: job.current_video,
                  extra: currentStepText(job),
                }
              : null
          }
          errorMessage={job.error_message}
          outputDir={job.output_dir}
        >
          {/* 每个视频的处理明细；片段结果统一在这里/历史弹窗里下钻查看，
              不再在页面上铺开一整个网格；失败的条目可单独重试 */}
          {job.items.length > 0 && (
            <Table
              style={{ marginTop: 12 }}
              rowKey="id"
              pagination={false}
              dataSource={job.items}
              columns={buildItemColumns(
                job,
                (item) => void openItemDetail(job, item),
                (item) => void retryItem(job, item),
              )}
            />
          )}
        </JobProgressCard>
      )}

      {/* ---------- 预览结果：切点清单 ---------- */}
      {summary && summary.total_scenes > 0 && (
        <Card
          title={
            <Space size={8}>
              <EyeOutlined style={{ color: 'var(--color-primary)' }} />
              切点预览
            </Space>
          }
          style={{ marginBottom: 16 }}
        >
          <Row gutter={16} style={{ marginBottom: 12 }}>
            <Col xs={12} sm={6}>
              <Statistic title="镜头总数" value={summary.total_scenes} suffix="个" />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic
                title="平均镜头时长"
                value={summary.average ?? 0}
                precision={2}
                suffix="秒"
              />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic
                title="最短镜头"
                value={summary.shortest ?? 0}
                precision={2}
                suffix="秒"
              />
            </Col>
            <Col xs={12} sm={6}>
              <Statistic
                title="最长镜头"
                value={summary.longest ?? 0}
                precision={2}
                suffix="秒"
              />
            </Col>
          </Row>

          <Collapse
            items={summary.items
              .filter((item) => (item.scenes?.length ?? 0) > 0)
              .map((item) => ({
                key: String(item.id),
                label: (
                  <Space>
                    <Text strong>{item.source_name}</Text>
                    <Tag>{item.scene_count} 个镜头</Tag>
                    {item.duration_seconds && <Tag>{formatDuration(item.duration_seconds)}</Tag>}
                  </Space>
                ),
                children: (
                  <Table
                    rowKey="number"
                    pagination={{ pageSize: 12, size: 'small', hideOnSinglePage: true }}
                    dataSource={item.scenes ?? []}
                    columns={scenesColumns}
                  />
                ),
              }))}
          />
        </Card>
      )}

      {summary && summary.total_scenes === 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="没有检测到切点"
          description="这些视频可能全片没有画面跳变（单镜头）。可以试试更敏感的模板，或改用「自定义」调低阈值。"
        />
      )}

      {/* ---------- 历史任务 ---------- */}
      <HistoryCard
        columns={historyColumns({
          onView: (id) => void openJobDetail(id),
          onCancel: (id) => void cancel(id),
          onDelete: (id) => void remove(id),
          purge,
        })}
        dataSource={history.items}
        loading={history.loading}
        onRefresh={history.reload}
        rowSelection={historyRowSelection}
        extra={
          <Popconfirm
            title={`删除这 ${history.selectedRowKeys.length} 条任务记录？`}
            description={
              <div>
                <div>删除后不可恢复。</div>
                {purge.checkbox}
              </div>
            }
            okText="删除"
            cancelText="取消"
            onConfirm={() => void batchRemove()}
            onOpenChange={(open) => {
              if (open) {
                purge.reset()
              }
            }}
          >
            <Button danger disabled={history.selectedRowKeys.length === 0}>
              批量删除{history.selectedRowKeys.length > 0 ? ` (${history.selectedRowKeys.length})` : ''}
            </Button>
          </Popconfirm>
        }
      />

      {/* ---------- 第一层：某条任务切了哪些视频 ---------- */}
      <Modal
        open={detailJobId !== null}
        title={
          <Space size={8}>
            <span>切片视频任务 #{detailJobId}</span>
            {detailJob && (
              <>
                <Tag>{detailJob.mode === 'preview' ? '预览切点' : '切分片段'}</Tag>
                <Tag color={JOB_STATUS_META[detailJob.status].color}>
                  {JOB_STATUS_META[detailJob.status].label}
                </Tag>
              </>
            )}
          </Space>
        }
        footer={null}
        width={960}
        onCancel={closeJobDetail}
        // 视频明细多时靠内容区内部滚动，别把弹窗撑出屏幕
        styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}
      >
        {detailLoading && !detailJob ? (
          <Flex justify="center" style={{ padding: 32 }}>
            <Spin />
          </Flex>
        ) : (
          detailJob && (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <DescriptionsBlock job={detailJob} />

              {detailJob.error_message && (
                <Alert
                  type="warning"
                  showIcon
                  message="部分内容未完成"
                  description={<Text style={{ fontSize: 12 }}>{detailJob.error_message}</Text>}
                />
              )}

              {/* 还在跑的时候这个弹窗自己会轮询，进度列是活的 */}
              <Table
                rowKey="id"
                pagination={false}
                dataSource={detailJob.items}
                locale={{ emptyText: <Empty description="这条任务没有视频明细" /> }}
                columns={buildItemColumns(
                detailJob,
                (item) => void openItemDetail(detailJob, item),
                (item) => void retryItem(detailJob, item),
              )}
              />
            </Space>
          )
        )}
      </Modal>

      {/* ---------- 第二层：某条视频切出的片段 ---------- */}
      <Modal
        open={detailItem !== null}
        title={
          detailItem && (
            <Space size={8}>
              <span>{detailItem.source_name}</span>
              {detailItemClips.length > 0 ? (
                <Tag color="blue">{detailItemClips.length} 个片段</Tag>
              ) : (
                (detailItem.scenes?.length ?? 0) > 0 && (
                  <Tag color="blue">{detailItem.scenes?.length} 个镜头</Tag>
                )
              )}
            </Space>
          )
        }
        footer={null}
        width={880}
        onCancel={() => setDetailItem(null)}
        // 片段网格动辄几十上百个，在内容区内部滚动，弹窗高度封顶
        styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}
      >
        {detailItem && (
          <Spin spinning={detailItemLoading}>
            {detailItemClips.length > 0 ? (
              // 播放范围就是这一条视频的片段，上一个/下一个走到头为止
              <ClipGrid clips={detailItemClips} onPlay={playClip} />
            ) : (detailItem.scenes?.length ?? 0) > 0 ? (
              // 预览任务只有切点，没有文件可播
              <Table
                rowKey="number"
                pagination={{ pageSize: 12, size: 'small', hideOnSinglePage: true }}
                dataSource={detailItem.scenes ?? []}
                columns={scenesColumns}
              />
            ) : (
              <Empty
                description={
                  detailItem.status === 'running'
                    ? '这条还在切，暂时没有片段'
                    : detailItem.single_shot
                      ? '全片没有画面跳变，切不出片段'
                      : '这条没有切出片段'
                }
              />
            )}
          </Spin>
        )}
      </Modal>

      {/* ---------- 片段播放器 ---------- */}
      <VideoPreviewModal
        open={player !== null}
        title={
          playingClip && (
            <Space size={8}>
              <PlayCircleOutlined />
              <span>片段 #{playingClip.index}</span>
              <Text type="secondary" style={{ fontWeight: 'normal', fontSize: 12 }}>
                {playingClip.name}
              </Text>
            </Space>
          )
        }
        src={playingClip?.video_url}
        // 换片段时换 key，让浏览器丢掉上一条的缓冲重新加载
        videoKey={playingClip?.index}
        // 底部这排是「上一个 / 下一个」：连播时不用退回网格一个个点
        footer={
          player && (
            <Flex justify="center" align="center" gap={12}>
              <Button disabled={player.index === 0} onClick={() => stepPlayer(-1)}>
                上一个
              </Button>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {player.index + 1} / {player.clips.length}
              </Text>
              <Button
                disabled={player.index >= player.clips.length - 1}
                onClick={() => stepPlayer(1)}
              >
                下一个
              </Button>
            </Flex>
          )
        }
        width={720}
        onClose={() => setPlayer(null)}
      />

      {/* ---------- 目录选择器 ---------- */}
      <DirectoryPicker
        open={picker.active !== null}
        title={picker.active === 'output' ? '选择输出目录' : '选择素材目录'}
        initialPath={picker.active === 'output' ? outputDir || dir.path : dir.path}
        onClose={picker.close}
        onSelect={(path) => {
          if (picker.active === 'output') {
            setOutputDir(path)
          } else {
            dir.setPath(path)
          }
        }}
      />
    </div>
  )
}

/** 任务条的统计信息（页面上和弹窗里共用） */
function DescriptionsBlock({ job }: { job: SceneJob }) {
  return (
    <Descriptions column={{ xs: 1, sm: 2 }}>
      <Descriptions.Item label="输入">{job.input_path}</Descriptions.Item>
      {job.output_dir && <Descriptions.Item label="输出">{job.output_dir}</Descriptions.Item>}
      <Descriptions.Item label="视频">
        {job.completed_videos} / {job.total_videos} 条完成
        {job.failed_videos > 0 && ` · ${job.failed_videos} 条失败`}
      </Descriptions.Item>
      <Descriptions.Item label="片段总数">{job.clip_count} 个</Descriptions.Item>
    </Descriptions>
  )
}

/**
 * 当前那条视频走到哪一步了（顶部提示条里的一句话）。
 *
 * 检测阶段刻意不给百分比：要跑多少帧得整条过完才知道，编一个分母出来就是
 * 假装精确 —— 后端也是同样的口径（见 scene_runner._refresh_job_progress）。
 */
function currentStepText(job: SceneJob): string {
  if (job.current_phase === 'detect') {
    return '正在检测镜头切换'
  }
  if (job.current_phase === 'split' && job.current_total_clips > 0) {
    return `正在切割第 ${job.current_clips}/${job.current_total_clips} 个片段`
  }
  return ''
}

/**
 * 单条视频的进度单元格。
 *
 * 只有正在跑的那一条有实时数据 —— 后端把「当前视频」的进度挂在任务上
 * （执行器串行，同一时刻只有一条在跑），这里按序号映射回它所在的那一行。
 * 没轮到的和已经跑完的都交给「镜头/片段」列去说，这里留一个破折号。
 */
function renderItemProgress(record: SceneJobItem, job: SceneJob | null): ReactNode {
  if (!job || record.status !== 'running' || record.index !== job.current_index) {
    return <Text type="secondary">—</Text>
  }

  // 检测中：分母还不存在。只表示「在动」，不画一根骗人的进度条。
  if (job.current_phase === 'detect') {
    return (
      <Tooltip title="检测要整条素材过完才知道有几个镜头，这里给不出百分比">
        <Space size={6}>
          <Spin />
          <Text style={{ fontSize: 12 }}>检测中</Text>
        </Space>
      </Tooltip>
    )
  }

  // 切割中：检测跑完分母才确定，这是全程唯一有真实百分比的阶段。
  if (job.current_phase === 'split' && job.current_total_clips > 0) {
    const percent = Math.min(
      100,
      Math.round((job.current_clips / job.current_total_clips) * 100),
    )
    return (
      <Flex vertical>
        <Text style={{ fontSize: 12 }}>
          切割中 {job.current_clips}/{job.current_total_clips}
        </Text>
        <Progress percent={percent} showInfo={false} style={{ marginBottom: 0 }} />
      </Flex>
    )
  }


  // 子进程刚起，vct 还没吐出第一行标记
  return (
    <Text type="secondary" style={{ fontSize: 12 }}>
      启动中
    </Text>
  )
}

/**
 * 视频清单的列定义：页面上的进度卡和「查看」弹窗共用一套。
 *
 * 传了 onView / onRetry 才多一列「操作」—— 有结果（片段或切点）的行给
 * 下钻入口；任务已终态且条目失败时给重试入口。
 */
function buildItemColumns(
  job: SceneJob | null,
  onView?: (item: SceneJobItem) => void,
  onRetry?: (item: SceneJobItem) => void,
): ColumnsType<SceneJobItem> {
  const columns: ColumnsType<SceneJobItem> = [
    { title: '#', dataIndex: 'index', width: 48 },
    { title: '视频', dataIndex: 'source_name', ellipsis: true },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (status: keyof typeof ITEM_STATUS_META, record: SceneJobItem) => (
        <Space size={4}>
          <Tag color={ITEM_STATUS_META[status].color}>{ITEM_STATUS_META[status].label}</Tag>
          {record.single_shot && status === 'success' && (
            <Tooltip title="全片没有画面跳变，切不出片段是正常结果">
              <Tag>单镜头</Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      // 实时进度：勾了多条视频时，一行一条，用户能看出正在切的是哪一条、
      // 切到第几个了。数据来自后端轮询（vct 逐段上报）。
      title: '进度',
      key: 'progress',
      width: 148,
      render: (_: unknown, record: SceneJobItem) => renderItemProgress(record, job),
    },
    {
      title: '镜头/片段',
      width: 110,
      render: (_: unknown, record: SceneJobItem) =>
        record.clip_count > 0 || record.scene_count > 0
          ? `${record.scene_count} / ${record.clip_count}`
          : '—',
    },
    {
      title: '耗时',
      dataIndex: 'elapsed_seconds',
      width: 80,
      render: (value: number) => (value > 0 ? `${value}s` : '—'),
    },
    {
      title: '说明',
      dataIndex: 'error_message',
      ellipsis: true,
      render: (value: string) =>
        value ? (
          <Tooltip title={value}>
            <Text type="danger" style={{ fontSize: 12 }}>
              {value}
            </Text>
          </Tooltip>
        ) : (
          '—'
        ),
    },
  ]

  if (onView || onRetry) {
    columns.push({
      title: '操作',
      width: 96,
      render: (_: unknown, record: SceneJobItem) => {
        // 任务还在跑时重跑没有意义（后端也会拒），只留查看
        const retryable =
          onRetry !== undefined && record.status === 'failed' && job !== null && isTerminalStatus(job.status)
        const viewable = record.clip_count > 0 || (record.scenes?.length ?? 0) > 0
        if (!retryable && !viewable) {
          // 没东西可看的（还没轮到、单镜头）别给一排点了没反应的链接
          return <Text type="secondary">—</Text>
        }
        return (
          <Space size={12}>
            {retryable && (
              <Button type="link" style={{ padding: 0 }} onClick={() => onRetry?.(record)}>
                重试
              </Button>
            )}
            {viewable && onView && (
              <Button type="link" style={{ padding: 0 }} onClick={() => onView(record)}>
                查看
              </Button>
            )}
          </Space>
        )
      },
    })
  }

  return columns
}

/**
 * 片段网格：每条视频的片段弹窗用。
 *
 * 卡片不写死列数也不写死高度：流式排布（flex-wrap 按容器宽度自动换行），
 * 封面比例跟随视频宽高（后端 ffprobe 探测；旧任务没有就退 16:9），横屏竖屏
 * 各按各的画幅渲染。
 *
 * onPlay 传的是「整组片段 + 点的是第几个」，播放器要靠这个组来决定
 * 上一个 / 下一个走到哪儿为止 —— 传进来的就是那条视频自己的片段。
 */
function ClipGrid({
  clips,
  onPlay,
}: {
  clips: SceneClip[]
  onPlay: (clips: SceneClip[], index: number) => void
}) {
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'flex-start' }}>
      {clips.map((clip, position) => (
        // 一行固定 6 列：宽度 = (容器宽 - 5 个间距) / 6，flex 不伸不缩，
        // 最后一行剩几个就是几个、不会被拉宽。
        <Card
          key={clip.index}
          hoverable
          styles={{ body: { padding: 6 } }}
          style={{ flex: '0 0 calc((100% - 80px) / 6)' }}
          cover={
            // 封面点击即播放：把 Image 的放大预览关掉（preview={false}），
            // 缩略图对视频来说没什么可看的，用户要的是看它怎么切出来的。
            <Tooltip title="点击播放这一片">
              <div
                onClick={() => onPlay(clips, position)}
                style={{
                  position: 'relative',
                  background: '#000',
                  // 展示区高度完全由图片撑开：缩略图是后端按视频真实比例等比
                  // 缩出来的（scale=320:-2），图片多宽高区域就多宽高 —— 没有
                  // 宽高数据的旧任务也天然正确。后端给了 width/height 时先用
                  // aspectRatio 占住位，图片加载前不塌陷。
                  lineHeight: 0,
                  overflow: 'hidden',
                  cursor: 'pointer',
                  ...(clip.width && clip.height
                    ? { aspectRatio: `${clip.width} / ${clip.height}` }
                    : {}),
                }}
              >
                <Image
                  src={clip.thumb_url}
                  alt={clip.name}
                  preview={false}
                  width="100%"
                  style={{ display: 'block', objectFit: 'contain' }}
                  fallback={THUMB_FALLBACK}
                />
                <span style={PLAY_BADGE}>
                  <PlayCircleOutlined />
                </span>
              </div>
            </Tooltip>
          }
        >
          <Flex justify="space-between" align="center" gap={4}>
            <Text style={{ fontSize: 12 }}>#{clip.index}</Text>
            <Text type="secondary" style={{ fontSize: 11 }}>
              {formatBytes(clip.size_bytes)}
            </Text>
          </Flex>
          {/* 片段名换行展示不省略：文件名没有空格，得 break-all 才断得开 */}
          <Text
            type="secondary"
            style={{ fontSize: 11, display: 'block', wordBreak: 'break-all' }}
          >
            {clip.name}
          </Text>
        </Card>
      ))}
    </div>
  )
}

/** 缩略图加载失败时的占位图 */
const THUMB_FALLBACK =
  'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNjAiIGhlaWdodD0iOTYiPjxyZWN0IHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiIGZpbGw9IiMxYTFhMWEiLz48dGV4dCB4PSI1MCUiIHk9IjUwJSIgZmlsbD0iIzg4OCIgZm9udC1zaXplPSIxMiIgdGV4dC1hbmNob3I9Im1pZGRsZSIgZHk9Ii4zZW0iPuaXoOe8qeeVpTwvdGV4dD48L3N2Zz4='

/** 切点表格列定义 */
const scenesColumns: ColumnsType<{ number: number; start: number; end: number; duration: number }> = [
  { title: '镜头', dataIndex: 'number', width: 72 },
  {
    title: '开始',
    dataIndex: 'start',
    width: 100,
    render: (value: number) => `${value.toFixed(2)}s`,
  },
  {
    title: '结束',
    dataIndex: 'end',
    width: 100,
    render: (value: number) => `${value.toFixed(2)}s`,
  },
  {
    title: '时长',
    dataIndex: 'duration',
    width: 100,
    render: (value: number) => (
      <Text type={value < 1 ? 'warning' : undefined}>{value.toFixed(2)}s</Text>
    ),
  },
]

/** 历史任务表格列定义 */
function historyColumns(handlers: {
  onView: (jobId: number) => void
  onCancel: (jobId: number) => void
  onDelete: (jobId: number) => void
  purge: UsePurgeFilesResult
}): ColumnsType<SceneJob> {
  return [
    jobIdColumn<SceneJob>(),
    {
      title: '模式',
      dataIndex: 'mode',
      width: 88,
      render: (mode: string) => (mode === 'preview' ? '预览切点' : '切分片段'),
    },
    jobStatusColumn<SceneJob>(JOB_STATUS_META),
    jobInputColumn<SceneJob>(),
    { title: '视频', dataIndex: 'total_videos', width: 64 },
    { title: '片段', dataIndex: 'clip_count', width: 64 },
    jobCreatedColumn<SceneJob>(),
    jobActionsColumn<SceneJob>({
      isTerminal: (job) => isTerminalStatus(job.status),
      onView: handlers.onView,
      onCancel: handlers.onCancel,
      onDelete: handlers.onDelete,
      deleteDescription: '删除后不可恢复。',
      purge: handlers.purge,
    }),
  ]
}
