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

import { useCallback, useEffect, useMemo, useState } from 'react'
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
  Popconfirm,
  Progress,
  Radio,
  Row,
  Select,
  Space,
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
  SceneJobMode,
  SceneSummary,
  SceneTemplate,
} from '../types/scene'

const { Text, Title, Paragraph } = Typography

/** 轮询间隔：进度字段每 3 秒落库一次，1.5 秒轮询足够及时又不刷爆后端 */
const POLL_INTERVAL_MS = 1500

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
      } catch (error) {
        messageApi.error(error instanceof ApiError ? error.message : '初始化失败')
      }
      void loadHistory()
    })()
  }, [messageApi, loadHistory])

  // 输入目录变化时：列出该目录下的视频文件
  useEffect(() => {
    if (!inputPath) {
      setInputData(null)
      return
    }
    void (async () => {
      try {
        const data = await fetchDirectory(inputPath)
        setInputData({ path: data.path, entries: data.entries })
        setSelectedFiles([])
        // 输出目录默认跟随输入目录，避免切出来的文件不知道去哪了
        setOutputDir((current) => current || data.path)
      } catch (error) {
        setInputData(null)
        messageApi.error(error instanceof ApiError ? error.message : '读取目录失败')
      }
    })()
  }, [inputPath, messageApi])

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

  const videoEntries = useMemo(
    () => (inputData?.entries ?? []).filter((entry) => entry.is_video),
    [inputData],
  )

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
      void loadHistory()
      messageApi.success('已删除任务记录（磁盘上的片段文件保留）')
    } catch (error) {
      messageApi.error(error instanceof ApiError ? error.message : '删除失败')
    }
  }

  /** 查看历史任务详情 */
  const viewJob = async (jobId: number) => {
    try {
      const detail = await fetchSceneJob(jobId)
      setJob(detail)
      void loadResults(detail)
    } catch (error) {
      messageApi.error(error instanceof ApiError ? error.message : '读取任务失败')
    }
  }

  const missingDeps = env?.dependencies.filter((dep) => !dep.ok && dep.name !== 'ffprobe') ?? []
  const running = job !== null && !isTerminalStatus(job.status)

  return (
    <div className="page">
      {contextHolder}

      <div className="page__header">
        <Title level={3} style={{ marginBottom: 4 }}>
          ✂️ 智能镜头分割
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
                      <Text type="warning">该目录下没有可处理的视频文件</Text>
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
              onClick={() => start('preview')}
              loading={submitting}
              disabled={!env?.ready}
            >
              🔍 预览切点
            </Button>
            <Button
              type="primary"
              onClick={() => start('split')}
              loading={submitting}
              disabled={!env?.ready || running}
            >
              ✂️ 开始切分
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
                  {job.current_clips > 0 && ` · 这条已切出 ${job.current_clips} 个片段`}
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
              columns={[
                { title: '#', dataIndex: 'index', width: 48 },
                { title: '视频', dataIndex: 'source_name', ellipsis: true },
                {
                  title: '状态',
                  dataIndex: 'status',
                  width: 90,
                  render: (status: keyof typeof ITEM_STATUS_META, record) => (
                    <Space size={4}>
                      <Tag color={ITEM_STATUS_META[status].color}>
                        {ITEM_STATUS_META[status].label}
                      </Tag>
                      {record.single_shot && status === 'success' && (
                        <Tooltip title="全片没有画面跳变，切不出片段是正常结果">
                          <Tag>单镜头</Tag>
                        </Tooltip>
                      )}
                    </Space>
                  ),
                },
                {
                  title: '镜头/片段',
                  width: 110,
                  render: (_: unknown, record) =>
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
              ]}
            />
          )}
        </Card>
      )}

      {/* ---------- 预览结果：切点清单 ---------- */}
      {summary && summary.total_scenes > 0 && (
        <Card title="🔍 切点预览" size="small" style={{ marginBottom: 16 }}>
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
            <Space>
              <span>🎬 切分结果</span>
              <Tag color="blue">{clips.length} 个片段</Tag>
            </Space>
          }
          size="small"
          style={{ marginBottom: 16 }}
        >
          <Image.PreviewGroup>
            <Row gutter={[12, 12]}>
              {clips.map((clip) => (
                <Col key={clip.index} xs={12} sm={8} md={6} lg={4}>
                  <Card
                    size="small"
                    hoverable
                    styles={{ body: { padding: 8 } }}
                    cover={
                      <div
                        style={{
                          background: '#000',
                          height: 96,
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          overflow: 'hidden',
                        }}
                      >
                        <Image
                          src={clip.thumb_url}
                          alt={clip.name}
                          height={96}
                          style={{ objectFit: 'cover' }}
                          fallback="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNjAiIGhlaWdodD0iOTYiPjxyZWN0IHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiIGZpbGw9IiMxYTFhMWEiLz48dGV4dCB4PSI1MCUiIHk9IjUwJSIgZmlsbD0iIzg4OCIgZm9udC1zaXplPSIxMiIgdGV4dC1hbmNob3I9Im1pZGRsZSIgZHk9Ii4zZW0iPuaXoOe8qeeVpTwvdGV4dD48L3N2Zz4="
                        />
                      </div>
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
          </Image.PreviewGroup>
        </Card>
      )}

      {/* ---------- 历史任务 ---------- */}
      <Card
        title="📋 历史任务"
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
            onView: viewJob,
            onCancel: cancel,
            onDelete: remove,
          })}
        />
      </Card>

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
      width: 150,
      render: (_: unknown, record) => (
        <Space size={4}>
          <a onClick={() => handlers.onView(record.id)}>查看</a>
          {!isTerminalStatus(record.status) ? (
            <a onClick={() => handlers.onCancel(record.id)}>取消</a>
          ) : (
            <Popconfirm
              title="删除这条任务记录？"
              description="只删记录，已切出的片段文件会保留在磁盘上。"
              okText="删除"
              cancelText="取消"
              onConfirm={() => handlers.onDelete(record.id)}
            >
              <a>删除</a>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]
}
