/**
 * 智能镜头分割页面。
 *
 * 流程：选素材目录 → 选模板 → 预览切点 / 直接切分 → 看进度 → 看片段网格。
 *
 * 两个关键设计：
 * 1. 任务由后端异步执行，页面用轮询拿进度（前端 fetch 超时 15 秒，
 *    而切一条两分钟素材就要几分钟，同步接口必然超时）；
 * 2. 预览与切分是两个独立任务（preview 不写用户目录，只把切点存库）。
 */

import {
  AppstoreOutlined,
  EyeOutlined,
  HistoryOutlined,
  ScissorOutlined,
} from '@ant-design/icons'
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
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
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'

import DirectoryPicker from '../components/DirectoryPicker'
import { fetchDirectory } from '../api/filesystem'
import {
  cancelSceneJob,
  createSceneJob,
  deleteSceneJob,
  fetchSceneClips,
  fetchSceneEnvironment,
  fetchSceneJob,
  fetchSceneJobs,
  fetchSceneSummary,
  fetchSceneTemplates,
} from '../api/scene'
import { ApiError } from '../api/client'
import {
  DETECTOR_OPTIONS,
  ITEM_STATUS_META,
  JOB_STATUS_META,
  formatBytes,
  formatDuration,
  isTerminalStatus,
} from '../types/scene'
import type {
  FsEntry,
  SceneClip,
  SceneEnvironment,
  SceneJob,
  SceneJobItem,
  SceneJobMode,
  SceneSummary,
  SceneTemplate,
} from '../types/scene'

const { Text, Title, Paragraph } = Typography

/** 轮询间隔：进度字段每 3 秒落库一次，1.5 秒轮询足够及时又不刷爆后端 */
const POLL_INTERVAL_MS = 1500

/** 片段封面上的播放角标：常驻的半透明三角，提示这一片是可以点的 */
const PLAY_BADGE: CSSProperties = {
  position: 'absolute',
  inset: 0,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontSize: 22,
  color: 'rgba(255, 255, 255, 0.85)',
  textShadow: '0 1px 6px rgba(0, 0, 0, 0.8)',
  pointerEvents: 'none',
}

/**
 * 播放器的状态：正在放的那一片，以及它所属的那一组片段。
 *
 * 连列表一起记住，是为了让「上一个 / 下一个」知道该在哪儿走 —— 从页面
 * 的切分结果里点开，范围就是整个任务的片段；从某条视频的弹窗里点开，
 * 范围就只是那条视频自己的片段，不会串到别人家去。
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
  const [messageApi, contextHolder] = message.useMessage()

  // ---------- 环境与模板 ----------
  const [env, setEnv] = useState<SceneEnvironment | null>(null)
  const [templates, setTemplates] = useState<SceneTemplate[]>([])

  // ---------- 输入区 ----------
  const [inputPath, setInputPath] = useState('')
  const [inputData, setInputData] = useState<{ path: string; entries: FsEntry[] } | null>(null)
  const [selectedFiles, setSelectedFiles] = useState<string[]>([])
  const [outputDir, setOutputDir] = useState('')
  const [recursive, setRecursive] = useState(false)
  /** 当前打开的目录选择器：input / output / null */
  const [picker, setPicker] = useState<'input' | 'output' | null>(null)

  // ---------- 模板区 ----------
  const [templateKey, setTemplateKey] = useState('standard')
  const [custom, setCustom] = useState(DEFAULT_CUSTOM)
  /** 自定义模式下是否使用检测器默认阈值 */
  const [useDefaultThreshold, setUseDefaultThreshold] = useState(true)

  // ---------- 任务与结果 ----------
  const [job, setJob] = useState<SceneJob | null>(null)
  const [clips, setClips] = useState<SceneClip[]>([])
  const [summary, setSummary] = useState<SceneSummary | null>(null)
  const [history, setHistory] = useState<SceneJob[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
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

  /** 拉取历史任务列表 */
  const loadHistory = useCallback(async () => {
    setHistoryLoading(true)
    try {
      const data = await fetchSceneJobs({ page: 1, page_size: 10 })
      setHistory(data.items)
    } catch {
      // 历史列表加载失败不影响主流程，静默处理
    } finally {
      setHistoryLoading(false)
    }
  }, [])

  /** 拉取某个任务的结果（预览取切点汇总，切分取片段列表） */
  const loadResults = useCallback(async (target: SceneJob) => {
    try {
      if (target.mode === 'preview') {
        setSummary(await fetchSceneSummary(target.id))
        setClips([])
      } else {
        setClips(await fetchSceneClips(target.id))
        setSummary(null)
      }
    } catch {
      // 结果拉取失败不打扰用户，界面上会显示为空
    }
  }, [])

  // 首次进入：探测环境、拉模板与历史
  useEffect(() => {
    void (async () => {
      try {
        const [envData, templateData] = await Promise.all([
          fetchSceneEnvironment(),
          fetchSceneTemplates(),
        ])
        setEnv(envData)
        setTemplates(templateData)
        // 输入/输出默认停在素材目录的两个分段：materials/source → materials/clips。
        // 把视频拷进 source/ 打开页面就能勾选，切出的片段落在 clips/，
        // 不会跟原片混在一层。函数式更新 + 空值判断，避免覆盖用户在这两个
        // 请求返回前已经手动选好的目录。
        if (envData.default_input_dir) {
          setInputPath((current) => current || envData.default_input_dir)
        }
        if (envData.default_output_dir) {
          setOutputDir((current) => current || envData.default_output_dir)
        }
      } catch (error) {
        messageApi.error(error instanceof ApiError ? error.message : '初始化失败')
      }
      void loadHistory()
    })()
  }, [messageApi, loadHistory])

  /**
   * 列出输入目录下的视频文件。
   *
   * keepSelection 区分两种调用：切换目录时重新从零开始（清空勾选），
   * 手动「重新扫描」时保留勾选 —— 用户往往是拷完新素材顺手点一下，
   * 已经挑好的那几条不该被清掉（只保留确实还在目录里的）。
   */
  const loadInputDir = useCallback(
    async (path: string, keepSelection = false) => {
      try {
        const data = await fetchDirectory(path)
        setInputData({ path: data.path, entries: data.entries })
        if (keepSelection) {
          const available = new Set(
            data.entries.filter((entry) => entry.is_video).map((entry) => entry.name),
          )
          setSelectedFiles((current) => current.filter((name) => available.has(name)))
        } else {
          setSelectedFiles([])
        }
        // 输出目录不再跟随输入目录：它有自己的固定去处 materials/clips/
        // （初始值由环境自检带回），产物和原片分开，一眼能看出哪是哪
      } catch (error) {
        setInputData(null)
        messageApi.error(error instanceof ApiError ? error.message : '读取目录失败')
      }
    },
    [messageApi],
  )

  // 输入目录变化时：列出该目录下的视频文件
  useEffect(() => {
    if (!inputPath) {
      setInputData(null)
      return
    }
    void loadInputDir(inputPath)
  }, [inputPath, loadInputDir])

  // 轮询进度：任务结束（终态）后自动停止
  useEffect(() => {
    if (!job || isTerminalStatus(job.status)) {
      return
    }
    const jobId = job.id
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          const fresh = await fetchSceneJob(jobId)
          setJob(fresh)
          if (isTerminalStatus(fresh.status)) {
            void loadResults(fresh)
            void loadHistory()
          }
        } catch {
          // 单次轮询失败不打断，下个周期会重试
        }
      })()
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [job, loadResults, loadHistory])

  // 弹窗里那条任务还在跑时同样轮询：清单上的进度才不是打开那一刻的快照。
  // 依赖的是 id 和「在跑」这个布尔值（不是对象本身），否则每次拿到响应都会
  // 把定时器拆了重建。终态一到，这个副作用自己就停了。
  const detailJobRunning = detailJob !== null && !isTerminalStatus(detailJob.status)
  useEffect(() => {
    if (detailJobId === null || !detailJobRunning) {
      return
    }
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          setDetailJob(await fetchSceneJob(detailJobId))
        } catch {
          // 单次轮询失败不打断，下个周期会重试
        }
      })()
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [detailJobId, detailJobRunning])

  const videoEntries = useMemo(
    () => (inputData?.entries ?? []).filter((entry) => entry.is_video),
    [inputData],
  )

  /** 当前看的是不是素材目录的 source 分段 —— 空列表时的提示文案要分情况 */
  const isEmptySourceDir =
    Boolean(env?.default_input_dir) && inputData?.path === env?.default_input_dir

  const selectedTemplate = templates.find((item) => item.key === templateKey)

  /** 创建任务 */
  const start = async (mode: SceneJobMode) => {
    if (!inputPath) {
      messageApi.warning('请先选择素材目录')
      return
    }
    if (videoEntries.length === 0 && selectedFiles.length === 0) {
      messageApi.warning('该目录下没有可处理的视频文件')
      return
    }
    if (mode === 'split' && !outputDir) {
      messageApi.warning('请先选择输出目录')
      return
    }

    setSubmitting(true)
    try {
      const created = await createSceneJob({
        input_path: inputPath,
        mode,
        template: templateKey,
        recursive,
        ...(selectedFiles.length > 0 ? { files: selectedFiles } : {}),
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
      setJob(created)
      setClips([])
      setSummary(null)
      void loadHistory()
      messageApi.success(
        mode === 'preview' ? '已开始检测切点，请稍候' : '已开始切割，可以在下面看进度',
      )
    } catch (error) {
      messageApi.error(error instanceof ApiError ? error.message : '创建任务失败')
    } finally {
      setSubmitting(false)
    }
  }

  /** 取消任务 */
  const cancel = async (jobId: number) => {
    try {
      const updated = await cancelSceneJob(jobId)
      setJob((current) => (current?.id === jobId ? updated : current))
      void loadHistory()
      messageApi.info('已请求取消')
    } catch (error) {
      messageApi.error(error instanceof ApiError ? error.message : '取消失败')
    }
  }

  /** 删除任务记录 */
  const remove = async (jobId: number) => {
    try {
      await deleteSceneJob(jobId)
      if (job?.id === jobId) {
        setJob(null)
        setClips([])
        setSummary(null)
      }
      if (detailJobId === jobId) {
        // 正在弹窗里看这条：记录没了就别让它挂在那儿
        closeJobDetail()
      }
      void loadHistory()
      messageApi.success('已删除任务记录（磁盘上的片段文件保留）')
    } catch (error) {
      messageApi.error(error instanceof ApiError ? error.message : '删除失败')
    }
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
    try {
      setDetailJob(await fetchSceneJob(jobId))
    } catch (error) {
      // 读不到就整个收起来，免得留一个空弹窗在那儿
      setDetailJobId(null)
      messageApi.error(error instanceof ApiError ? error.message : '读取任务失败')
    } finally {
      setDetailLoading(false)
    }
  }

  /** 关掉第一层弹窗：顺手把第二层也关掉，别留个孤儿挂在后面 */
  const closeJobDetail = () => {
    setDetailJobId(null)
    setDetailJob(null)
    setDetailItem(null)
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
      messageApi.error(error instanceof ApiError ? error.message : '读取片段失败')
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

  const missingDeps = env?.dependencies.filter((dep) => !dep.ok && dep.name !== 'ffprobe') ?? []
  const running = job !== null && !isTerminalStatus(job.status)
  /** 正在放的那一片 */
  const playingClip = player ? player.clips[player.index] : null

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
      {env && !env.ready && (
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
          <Card title="① 选择素材与输出位置" size="small" style={{ height: '100%' }}>
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <div>
                <Text type="secondary">素材目录</Text>
                <Space.Compact style={{ width: '100%', marginTop: 4 }}>
                  <Button onClick={() => setPicker('input')}>选择目录</Button>
                  <Text
                    style={{
                      flex: 1,
                      padding: '4px 11px',
                      border: '1px solid var(--color-border)',
                      borderRadius: 6,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      color: inputPath ? undefined : 'var(--color-text-muted)',
                    }}
                  >
                    {inputPath || '尚未选择'}
                  </Text>
                </Space.Compact>
              </div>

              {inputData && (
                <div>
                  <Flex justify="space-between" align="center" style={{ marginBottom: 6 }}>
                    <Text type="secondary">
                      可处理视频 {videoEntries.length} 条
                      {selectedFiles.length > 0 && ` · 已勾选 ${selectedFiles.length} 条`}
                    </Text>
                    <Space size={4}>
                      <Button
                        size="small"
                        onClick={() => void loadInputDir(inputPath, true)}
                        disabled={!inputPath}
                      >
                        重新扫描
                      </Button>
                      <Checkbox
                        checked={selectedFiles.length === 0}
                        onChange={(event) =>
                          setSelectedFiles(event.target.checked ? [] : videoEntries.map((v) => v.name))
                        }
                      >
                        未勾选=全部
                      </Checkbox>
                      <Tag color="blue">递归子目录</Tag>
                      <Switch size="small" checked={recursive} onChange={setRecursive} />
                    </Space>
                  </Flex>
                  <div
                    style={{
                      maxHeight: 168,
                      overflow: 'auto',
                      border: '1px solid var(--color-border)',
                      borderRadius: 8,
                      padding: '8px 12px',
                    }}
                  >
                    {videoEntries.length === 0 ? (
                      isEmptySourceDir ? (
                        // source/ 第一次用的时候必然是空的，这里直接说清「往哪儿放」
                        <Text type="warning">
                          还没有素材 —— 把视频拷进 {inputData.path}，再点「重新扫描」
                        </Text>
                      ) : (
                        <Text type="warning">该目录下没有可处理的视频文件</Text>
                      )
                    ) : (
                      <Checkbox.Group
                        value={selectedFiles}
                        onChange={(values) => setSelectedFiles(values as string[])}
                        style={{ display: 'flex', flexDirection: 'column', gap: 6 }}
                      >
                        {videoEntries.map((entry) => (
                          <Checkbox key={entry.name} value={entry.name}>
                            <Text style={{ fontSize: 13 }}>{entry.name}</Text>
                            <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                              {entry.size_bytes !== null ? formatBytes(entry.size_bytes) : ''}
                            </Text>
                          </Checkbox>
                        ))}
                      </Checkbox.Group>
                    )}
                  </div>
                </div>
              )}

              <div>
                <Text type="secondary">输出目录（切出的片段放这里）</Text>
                <Space.Compact style={{ width: '100%', marginTop: 4 }}>
                  <Button onClick={() => setPicker('output')}>选择目录</Button>
                  <Text
                    style={{
                      flex: 1,
                      padding: '4px 11px',
                      border: '1px solid var(--color-border)',
                      borderRadius: 6,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      color: outputDir ? undefined : 'var(--color-text-muted)',
                    }}
                  >
                    {outputDir || '尚未选择'}
                  </Text>
                </Space.Compact>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  每条视频的片段会放进独立的子目录（视频名_scenes），不会混在一起
                </Text>
              </div>
            </Space>
          </Card>
        </Col>

        <Col xs={24} lg={10}>
          <Card title="② 选择切割模板" size="small" style={{ height: '100%' }}>
            <Radio.Group
              value={templateKey}
              onChange={(event) => setTemplateKey(event.target.value as string)}
              style={{ width: '100%' }}
            >
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                {templates.map((template) => (
                  <Card
                    key={template.key}
                    size="small"
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
                      size="small"
                    />
                  </div>
                  <Flex align="center" gap={8}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      使用检测器默认阈值
                    </Text>
                    <Switch
                      size="small"
                      checked={useDefaultThreshold}
                      onChange={setUseDefaultThreshold}
                    />
                  </Flex>
                  {!useDefaultThreshold && (
                    <Flex align="center" gap={8}>
                      <Text type="secondary" style={{ fontSize: 12, width: 72 }}>
                        阈值
                      </Text>
                      <InputNumber
                        size="small"
                        min={0.1}
                        max={100}
                        step={0.5}
                        value={custom.threshold ?? 3}
                        onChange={(value) =>
                          setCustom((c) => ({ ...c, threshold: value ?? 3 }))
                        }
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
                      size="small"
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
                      size="small"
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
      <Card size="small" style={{ marginBottom: 16 }}>
        <Flex align="center" justify="space-between" wrap gap={12}>
          <Space>
            <Button
              icon={<EyeOutlined />}
              onClick={() => start('preview')}
              loading={submitting}
              disabled={!env?.ready}
            >
              预览切点
            </Button>
            <Button
              type="primary"
              icon={<ScissorOutlined />}
              onClick={() => start('split')}
              loading={submitting}
              disabled={!env?.ready || running}
            >
              开始切分
            </Button>
            {running && (
              <Popconfirm
                title="取消当前任务？"
                description="已切出的片段会保留，未处理的视频会被跳过。"
                okText="取消任务"
                cancelText="继续跑"
                onConfirm={() => job && cancel(job.id)}
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
        <Card
          size="small"
          title={
            <Space>
              <span>任务 #{job.id}</span>
              <Tag color={JOB_STATUS_META[job.status].color}>
                {JOB_STATUS_META[job.status].label}
              </Tag>
              <Tag>{job.mode === 'preview' ? '预览切点' : '切分片段'}</Tag>
            </Space>
          }
          style={{ marginBottom: 16 }}
        >
          <Progress
            percent={job.progress_percent}
            status={
              job.status === 'failed'
                ? 'exception'
                : running
                  ? 'active'
                  : job.status === 'cancelled'
                    ? 'normal'
                    : 'success'
            }
          />
          <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 4 }} style={{ marginTop: 8 }}>
            <Descriptions.Item label="视频进度">
              {job.completed_videos} / {job.total_videos} 条
            </Descriptions.Item>
            <Descriptions.Item label="已切出片段">{job.clip_count} 个</Descriptions.Item>
            <Descriptions.Item label="检测镜头数">{job.scene_count} 个</Descriptions.Item>
            <Descriptions.Item label="耗时">
              {formatDuration(
                job.started_at
                  ? (Date.now() - new Date(job.started_at).getTime()) / 1000
                  : null,
              )}
            </Descriptions.Item>
          </Descriptions>

          {job.current_video && running && (
            <Alert
              type="info"
              showIcon
              style={{ marginTop: 8 }}
              message={
                <span>
                  正在处理第 {job.current_index}/{job.total_videos} 条：
                  <Text strong>{job.current_video}</Text>
                  {currentStepText(job) && ` · ${currentStepText(job)}`}
                </span>
              }
            />
          )}

          {job.error_message && (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 8 }}
              message="部分内容未完成"
              description={<Text style={{ fontSize: 12 }}>{job.error_message}</Text>}
            />
          )}

          {job.output_dir && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              输出位置：<Text code>{job.output_dir}</Text>
            </Text>
          )}

          {/* 每个视频的处理明细 */}
          {job.items.length > 0 && (
            <Table
              size="small"
              style={{ marginTop: 12 }}
              rowKey="id"
              pagination={false}
              dataSource={job.items}
              columns={itemColumns(job)}
            />
          )}
        </Card>
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
          size="small"
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
            size="small"
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
                    size="small"
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

      {/* ---------- 切分结果：片段网格 ---------- */}
      {clips.length > 0 && (
        <Card
          title={
            <Space size={8}>
              <AppstoreOutlined style={{ color: 'var(--color-primary)' }} />
              <span>切分结果</span>
              <Tag color="blue">{clips.length} 个片段</Tag>
            </Space>
          }
          size="small"
          style={{ marginBottom: 16 }}
        >
          <ClipGrid clips={clips} onPlay={playClip} />
        </Card>
      )}

      {/* ---------- 历史任务 ---------- */}
      <Card
        title={
          <Space size={8}>
            <HistoryOutlined style={{ color: 'var(--color-primary)' }} />
            历史任务
          </Space>
        }
        size="small"
        extra={
          <Button size="small" onClick={() => loadHistory()} loading={historyLoading}>
            刷新
          </Button>
        }
      >
        <Table
          size="small"
          rowKey="id"
          loading={historyLoading}
          dataSource={history}
          pagination={false}
          locale={{ emptyText: <Empty description="还没有任务记录" /> }}
          columns={historyColumns({
            onView: openJobDetail,
            onCancel: cancel,
            onDelete: remove,
          })}
        />
      </Card>

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
      >
        {detailLoading && !detailJob ? (
          <Flex justify="center" style={{ padding: 32 }}>
            <Spin />
          </Flex>
        ) : (
          detailJob && (
            <Space direction="vertical" size={12} style={{ width: '100%' }}>
              <Descriptions size="small" column={{ xs: 1, sm: 2 }}>
                <Descriptions.Item label="输入">{detailJob.input_path}</Descriptions.Item>
                {detailJob.output_dir && (
                  <Descriptions.Item label="输出">{detailJob.output_dir}</Descriptions.Item>
                )}
                <Descriptions.Item label="视频">
                  {detailJob.completed_videos} / {detailJob.total_videos} 条完成
                  {detailJob.failed_videos > 0 && ` · ${detailJob.failed_videos} 条失败`}
                </Descriptions.Item>
                <Descriptions.Item label="片段总数">
                  {detailJob.clip_count} 个
                </Descriptions.Item>
              </Descriptions>

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
                size="small"
                rowKey="id"
                pagination={false}
                dataSource={detailJob.items}
                locale={{ emptyText: <Empty description="这条任务没有视频明细" /> }}
                columns={itemColumns(detailJob, (item) => void openItemDetail(detailJob, item))}
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
      >
        {detailItem && (
          <Spin spinning={detailItemLoading}>
            {detailItemClips.length > 0 ? (
              // 播放范围就是这一条视频的片段，上一个/下一个走到头为止
              <ClipGrid clips={detailItemClips} onPlay={playClip} />
            ) : (detailItem.scenes?.length ?? 0) > 0 ? (
              // 预览任务只有切点，没有文件可播
              <Table
                size="small"
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
      <Modal
        open={player !== null}
        title={
          playingClip && (
            <Space size={8}>
              <span>▶ 片段 #{playingClip.index}</span>
              <Text type="secondary" style={{ fontWeight: 'normal', fontSize: 12 }}>
                {playingClip.name}
              </Text>
            </Space>
          )
        }
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
        onCancel={() => setPlayer(null)}
        // 关掉就卸载 <video>：否则弹窗关了后台还在下载、还在出声
        destroyOnHidden
      >
        {playingClip && (
          <video
            // 换片段时换 key，让浏览器丢掉上一条的缓冲重新加载
            key={playingClip.index}
            src={playingClip.video_url}
            controls
            autoPlay
            style={{ width: '100%', maxHeight: '70vh', background: '#000', borderRadius: 6 }}
          />
        )}
      </Modal>

      {/* ---------- 目录选择器 ---------- */}
      <DirectoryPicker
        open={picker !== null}
        title={picker === 'output' ? '选择输出目录' : '选择素材目录'}
        initialPath={picker === 'output' ? outputDir || inputPath : inputPath}
        onClose={() => setPicker(null)}
        onSelect={(path) => {
          if (picker === 'output') {
            setOutputDir(path)
          } else {
            setInputPath(path)
          }
        }}
      />
    </div>
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
function renderItemProgress(record: SceneJobItem, job: SceneJob): ReactNode {
  if (record.status !== 'running' || record.index !== job.current_index) {
    return <Text type="secondary">—</Text>
  }

  // 检测中：分母还不存在。只表示「在动」，不画一根骗人的进度条。
  if (job.current_phase === 'detect') {
    return (
      <Tooltip title="检测要整条素材过完才知道有几个镜头，这里给不出百分比">
        <Space size={6}>
          <Spin size="small" />
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
        <Progress
          percent={percent}
          size="small"
          showInfo={false}
          style={{ marginBottom: 0 }}
        />
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
 * 传了 onView 才多一列「操作」—— 页面上那条任务的结果已经在下面的网格里
 * 铺开了，不需要再下钻一层。
 */
function itemColumns(
  job: SceneJob,
  onView?: (item: SceneJobItem) => void,
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

  if (onView) {
    columns.push({
      title: '操作',
      width: 64,
      render: (_: unknown, record: SceneJobItem) =>
        // 没东西可看的（还没轮到、单镜头、失败）别给一个点了没反应的链接
        record.clip_count > 0 || (record.scenes?.length ?? 0) > 0 ? (
          <Button type="link" size="small" style={{ padding: 0 }} onClick={() => onView(record)}>
            查看
          </Button>
        ) : (
          <Text type="secondary">—</Text>
        ),
    })
  }

  return columns
}

/**
 * 片段网格：页面上的「切分结果」和每条视频的片段弹窗共用一套渲染。
 *
 * onPlay 传的是「整组片段 + 点的是第几个」，播放器要靠这个组来决定
 * 上一个 / 下一个走到哪儿为止 —— 弹窗里传的就是那条视频自己的片段。
 */
function ClipGrid({
  clips,
  onPlay,
}: {
  clips: SceneClip[]
  onPlay: (clips: SceneClip[], index: number) => void
}) {
  return (
    <Row gutter={[12, 12]}>
      {clips.map((clip, position) => (
        // 按容器宽度铺满，而不是按视口切死的 1/6：同一个组件既用在整页的
        // 「切分结果」里，也用在 880 宽的弹窗里，切死的话弹窗里只有 3 个片段
        // 时会在右边空出一大片。maxWidth 兜住最后一行只剩一个时被拉成巨幅。
        <Col key={clip.index} flex="1 1 200px" style={{ maxWidth: 320 }}>
          <Card
            size="small"
            hoverable
            styles={{ body: { padding: 8 } }}
            cover={
              // 封面点击即播放：把 Image 的放大预览关掉（preview={false}），
              // 缩略图对视频来说没什么可看的，用户要的是看它怎么切出来的。
              <Tooltip title="点击播放这一片">
                <div
                  onClick={() => onPlay(clips, position)}
                  style={{
                    position: 'relative',
                    background: '#000',
                    height: 96,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    overflow: 'hidden',
                    cursor: 'pointer',
                  }}
                >
                  <Image
                    src={clip.thumb_url}
                    alt={clip.name}
                    preview={false}
                    height={96}
                    style={{ objectFit: 'cover' }}
                    fallback="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNjAiIGhlaWdodD0iOTYiPjxyZWN0IHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiIGZpbGw9IiMxYTFhMWEiLz48dGV4dCB4PSI1MCUiIHk9IjUwJSIgZmlsbD0iIzg4OCIgZm9udC1zaXplPSIxMiIgdGV4dC1hbmNob3I9Im1pZGRsZSIgZHk9Ii4zZW0iPuaXoOe8qeeVpTwvdGV4dD48L3N2Zz4="
                  />
                  <span style={PLAY_BADGE}>▶</span>
                </div>
              </Tooltip>
            }
          >
            <Flex justify="space-between" align="center">
              <Text style={{ fontSize: 12 }} ellipsis>
                #{clip.index}
              </Text>
              <Text type="secondary" style={{ fontSize: 11 }}>
                {formatBytes(clip.size_bytes)}
              </Text>
            </Flex>
            <Tooltip title={`${clip.source_name} → ${clip.name}`}>
              <Text type="secondary" style={{ fontSize: 11 }} ellipsis>
                {clip.name}
              </Text>
            </Tooltip>
          </Card>
        </Col>
      ))}
    </Row>
  )
}

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
}): ColumnsType<SceneJob> {
  return [
    { title: 'ID', dataIndex: 'id', width: 64 },
    {
      title: '模式',
      dataIndex: 'mode',
      width: 88,
      render: (mode: string) => (mode === 'preview' ? '预览切点' : '切分片段'),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 96,
      render: (status: keyof typeof JOB_STATUS_META) => (
        <Tooltip title={JOB_STATUS_META[status].hint}>
          <Tag color={JOB_STATUS_META[status].color}>{JOB_STATUS_META[status].label}</Tag>
        </Tooltip>
      ),
    },
    {
      title: '输入',
      dataIndex: 'input_path',
      ellipsis: true,
      render: (value: string) => (
        <Tooltip title={value}>
          <Text style={{ fontSize: 12 }}>{value.split('/').pop()}</Text>
        </Tooltip>
      ),
    },
    { title: '视频', dataIndex: 'total_videos', width: 64 },
    { title: '片段', dataIndex: 'clip_count', width: 64 },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 150,
      render: (value: string) => (
        <Text style={{ fontSize: 12 }}>{new Date(value).toLocaleString('zh-CN')}</Text>
      ),
    },
    {
      title: '操作',
      // 三个操作按钮（查看 / 取消 / 删除）并排，宽度按最宽的那种状态留够
      width: 180,
      render: (_: unknown, record) => (
        <Space size={4}>
          <Button
            type="link"
            size="small"
            style={{ padding: 0 }}
            onClick={() => handlers.onView(record.id)}
          >
            查看
          </Button>
          {!isTerminalStatus(record.status) ? (
            <Button
              type="link"
              size="small"
              style={{ padding: 0 }}
              onClick={() => handlers.onCancel(record.id)}
            >
              取消
            </Button>
          ) : (
            <Popconfirm
              title="删除这条任务记录？"
              description="只删记录，已切出的片段文件会保留在磁盘上。"
              okText="删除"
              cancelText="取消"
              onConfirm={() => handlers.onDelete(record.id)}
            >
              <Button type="link" size="small" danger style={{ padding: 0 }}>
                删除
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]
}
