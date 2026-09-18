/**
 * 视频字幕提取页面。
 *
 * 流程：选素材目录 → 勾选视频 → 选识别引擎/语言 → 开始提取 → 看进度 → 预览字幕文本。
 *
 * 与镜头分割同构的两个关键设计：
 * 1. 任务由后端异步执行，页面用轮询拿进度（转写一条几分钟的视频可能要几分钟，
 *    同步接口必然超时）；
 * 2. 字幕是「一条视频一份 .srt」，产物页就是一个文件清单 + 文本预览，
 *    没有片段网格、没有播放器。
 *
 * 进页面先探测 VideoCaptioner 环境：没装就显示安装指引并禁用「开始提取」，
 * 装了就在顶部给一条绿色的版本信息。
 */

import {
  CopyOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  HistoryOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Empty,
  Flex,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Spin,
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
  cancelSubtitleJob,
  createSubtitleJob,
  deleteSubtitleJob,
  fetchSubtitleEnvironment,
  fetchSubtitleFiles,
  fetchSubtitleJob,
  fetchSubtitleJobs,
  fetchSubtitleText,
} from '../api/subtitle'
import { ApiError } from '../api/client'
import {
  ITEM_STATUS_META,
  JOB_STATUS_META,
  formatBytes,
  formatElapsed,
  isTerminalStatus,
} from '../types/subtitle'
import type {
  SubtitleEnvironment,
  SubtitleFile,
  SubtitleJob,
  SubtitleJobItem,
  SubtitleText,
} from '../types/subtitle'
import type { FsEntry } from '../types/scene'

const { Text, Title, Paragraph } = Typography

/** 轮询间隔：进度字段每 3 秒落库一次，1.5 秒轮询足够及时又不刷爆后端 */
const POLL_INTERVAL_MS = 1500

/** 识别语言候选项：留空表示自动检测（后端默认值） */
const LANGUAGE_OPTIONS = [
  { value: '', label: '自动检测' },
  { value: 'zh', label: '中文' },
  { value: 'en', label: '英文' },
  { value: 'ja', label: '日文' },
  { value: 'ko', label: '韩文' },
]

export default function SubtitleExtract() {
  const [messageApi, contextHolder] = message.useMessage()

  // ---------- 环境 ----------
  const [env, setEnv] = useState<SubtitleEnvironment | null>(null)
  const [envLoading, setEnvLoading] = useState(false)

  // ---------- 输入区 ----------
  const [inputPath, setInputPath] = useState('')
  const [inputData, setInputData] = useState<{ path: string; entries: FsEntry[] } | null>(null)
  const [selectedFiles, setSelectedFiles] = useState<string[]>([])
  const [outputDir, setOutputDir] = useState('')
  const [recursive, setRecursive] = useState(false)
  /** 当前打开的目录选择器：input / output / null */
  const [picker, setPicker] = useState<'input' | 'output' | null>(null)

  // ---------- 转写参数 ----------
  const [asr, setAsr] = useState('bijian')
  const [language, setLanguage] = useState('')

  // ---------- 任务与结果 ----------
  const [job, setJob] = useState<SubtitleJob | null>(null)
  const [subtitles, setSubtitles] = useState<SubtitleFile[]>([])
  const [history, setHistory] = useState<SubtitleJob[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  // ---------- 字幕预览弹窗 ----------
  const [preview, setPreview] = useState<SubtitleText | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)

  /** 拉取历史任务列表 */
  const loadHistory = useCallback(async () => {
    setHistoryLoading(true)
    try {
      const data = await fetchSubtitleJobs({ page: 1, page_size: 10 })
      setHistory(data.items)
    } catch {
      // 历史列表加载失败不影响主流程，静默处理
    } finally {
      setHistoryLoading(false)
    }
  }, [])

  /** 拉取某条任务已产出的字幕清单 */
  const loadSubtitles = useCallback(async (jobId: number) => {
    try {
      setSubtitles(await fetchSubtitleFiles(jobId))
    } catch {
      // 拉取失败不打扰用户，界面上会显示为空
    }
  }, [])

  /** 探测环境（refresh=true 绕过缓存，给「重新检测」按钮用） */
  const loadEnv = useCallback(
    async (refresh = false) => {
      setEnvLoading(true)
      try {
        const envData = await fetchSubtitleEnvironment(refresh)
        setEnv(envData)
        // 输入/输出默认停在素材目录的两个分段：materials/source → materials/subtitle。
        // 函数式更新 + 空值判断，避免覆盖用户在请求返回前已经手动选好的目录。
        if (envData.default_input_dir) {
          setInputPath((current) => current || envData.default_input_dir)
        }
        if (envData.default_output_dir) {
          setOutputDir((current) => current || envData.default_output_dir)
        }
      } catch (error) {
        messageApi.error(error instanceof ApiError ? error.message : '环境探测失败')
      } finally {
        setEnvLoading(false)
      }
    },
    [messageApi],
  )

  // 首次进入：探测环境、拉历史
  useEffect(() => {
    void loadEnv()
    void loadHistory()
  }, [loadEnv, loadHistory])

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
          const fresh = await fetchSubtitleJob(jobId)
          setJob(fresh)
          if (isTerminalStatus(fresh.status)) {
            void loadSubtitles(fresh.id)
            void loadHistory()
          }
        } catch {
          // 单次轮询失败不打断，下个周期会重试
        }
      })()
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [job, loadSubtitles, loadHistory])

  const videoEntries = useMemo(
    () => (inputData?.entries ?? []).filter((entry) => entry.is_video),
    [inputData],
  )

  /** 当前看的是不是素材目录的 source 分段 —— 空列表时的提示文案要分情况 */
  const isEmptySourceDir =
    Boolean(env?.default_input_dir) && inputData?.path === env?.default_input_dir

  /** 创建任务 */
  const start = async () => {
    if (!inputPath) {
      messageApi.warning('请先选择素材目录')
      return
    }
    if (videoEntries.length === 0 && selectedFiles.length === 0) {
      messageApi.warning('该目录下没有可处理的视频文件')
      return
    }

    setSubmitting(true)
    try {
      const created = await createSubtitleJob({
        input_path: inputPath,
        recursive,
        asr,
        ...(language ? { language } : {}),
        ...(outputDir ? { output_dir: outputDir } : {}),
        ...(selectedFiles.length > 0 ? { files: selectedFiles } : {}),
      })
      setJob(created)
      setSubtitles([])
      void loadHistory()
      messageApi.success('已开始提取字幕，可以在下面看进度')
    } catch (error) {
      messageApi.error(error instanceof ApiError ? error.message : '创建任务失败')
    } finally {
      setSubmitting(false)
    }
  }

  /** 取消任务 */
  const cancel = async (jobId: number) => {
    try {
      const updated = await cancelSubtitleJob(jobId)
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
      await deleteSubtitleJob(jobId)
      if (job?.id === jobId) {
        setJob(null)
        setSubtitles([])
      }
      void loadHistory()
      messageApi.success('已删除任务记录（磁盘上的字幕文件保留）')
    } catch (error) {
      messageApi.error(error instanceof ApiError ? error.message : '删除失败')
    }
  }

  /** 打开字幕预览 */
  const openPreview = async (jobId: number, index: number) => {
    setPreviewLoading(true)
    try {
      setPreview(await fetchSubtitleText(jobId, index))
    } catch (error) {
      messageApi.error(error instanceof ApiError ? error.message : '读取字幕失败')
    } finally {
      setPreviewLoading(false)
    }
  }

  /** 从历史记录点开一条任务看结果 */
  const openHistoryJob = async (jobId: number) => {
    try {
      const detail = await fetchSubtitleJob(jobId)
      setJob(detail)
      void loadSubtitles(jobId)
    } catch (error) {
      messageApi.error(error instanceof ApiError ? error.message : '读取任务失败')
    }
  }

  /** 复制到剪贴板 */
  const copyText = (text: string) => {
    void navigator.clipboard.writeText(text).then(
      () => messageApi.success('已复制'),
      () => messageApi.error('复制失败，请手动选择复制'),
    )
  }

  const running = job !== null && !isTerminalStatus(job.status)
  const canSubmit = Boolean(env?.ready) && !running && !submitting

  return (
    <div className="page">
      {contextHolder}

      <div className="page__header">
        <Title level={3} style={{ marginBottom: 4 }}>
          <FileTextOutlined style={{ marginRight: 8 }} />
          视频字幕提取
        </Title>
        <Paragraph type="secondary" style={{ marginBottom: 0 }}>
          把视频里的语音转成 .srt 字幕文件。选一批视频，字幕统一落到输出目录。
        </Paragraph>
      </div>

      {/* ---------- 环境自检 ---------- */}
      {env && !env.ready && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={
            env.installed
              ? 'ffmpeg 未找到，转写会失败'
              : `未检测到 VideoCaptioner（${env.platform_label}）`
          }
          description={
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              {env.detail && <Text>{env.detail}</Text>}
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
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={envLoading}
                onClick={() => void loadEnv(true)}
              >
                重新检测
              </Button>
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
            <Space size={8}>
              <span>环境正常</span>
              {env.version && <Tag color="green">VideoCaptioner {env.version}</Tag>}
              <Text type="secondary" style={{ fontSize: 12 }}>
                配置：{env.config_file}
              </Text>
            </Space>
          }
        />
      )}

      {/* ---------- 输入与参数 ---------- */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} lg={14}>
          <Card
            title={
              <Space size={8}>
                <FolderOpenOutlined style={{ color: 'var(--color-primary)' }} />
                选择素材与输出位置
              </Space>
            }
            style={{ height: '100%' }}
          >
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
                        onClick={() => void loadInputDir(inputPath, true)}
                        disabled={!inputPath}
                      >
                        重新扫描
                      </Button>
                      <Checkbox
                        checked={selectedFiles.length === 0}
                        onChange={(event) =>
                          setSelectedFiles(
                            event.target.checked ? [] : videoEntries.map((v) => v.name),
                          )
                        }
                      >
                        未勾选=全部
                      </Checkbox>
                      <Tag color="blue">递归子目录</Tag>
                      <Switch checked={recursive} onChange={setRecursive} />
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
                <Text type="secondary">输出目录（字幕文件放这里）</Text>
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
                    {outputDir || '默认（materials/subtitle）'}
                  </Text>
                </Space.Compact>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  每条视频生成一个同名 .srt 文件，重名自动加 -2、-3 后缀
                </Text>
              </div>
            </Space>
          </Card>
        </Col>

        <Col xs={24} lg={10}>
          <Card
            title={
              <Space size={8}>
                <FileTextOutlined style={{ color: 'var(--color-primary)' }} />
                转写参数
              </Space>
            }
            style={{ height: '100%' }}
          >
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  识别引擎
                </Text>
                <Select
                  value={asr}
                  onChange={setAsr}
                  style={{ width: '100%', marginTop: 4 }}
                  options={(env?.asr_engines ?? []).map((engine) => ({
                    value: engine.key,
                    label: `${engine.name}${engine.recommended ? '（推荐）' : ''}${engine.requires_key ? '（需配置 key）' : ''}`,
                  }))}
                />
                {env?.asr_engines && (
                  <div style={{ marginTop: 4 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {env.asr_engines.find((e) => e.key === asr)?.summary ?? ''}
                    </Text>
                  </div>
                )}
              </div>

              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  识别语言
                </Text>
                <Select
                  value={language}
                  onChange={setLanguage}
                  options={LANGUAGE_OPTIONS}
                  style={{ width: '100%', marginTop: 4 }}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  留「自动检测」即可，除非视频里的语言比较冷门、检测结果不理想
                </Text>
              </div>

              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  输出格式
                </Text>
                <div style={{ marginTop: 4 }}>
                  <Tag>srt</Tag>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    所有剪辑软件与文本工具都认的字幕格式
                  </Text>
                </div>
              </div>
            </Space>
          </Card>
        </Col>
      </Row>

      {/* ---------- 动作区 ---------- */}
      <Card style={{ marginBottom: 16 }}>
        <Flex align="center" justify="space-between" wrap gap={12}>
          <Space>
            <Button
              type="primary"
              icon={<FileTextOutlined />}
              onClick={() => void start()}
              loading={submitting}
              disabled={!canSubmit}
            >
              开始提取
            </Button>
            {running && (
              <Popconfirm
                title="取消当前任务？"
                description="已生成的字幕会保留，未处理的视频会被跳过。"
                okText="取消任务"
                cancelText="继续跑"
                onConfirm={() => job && cancel(job.id)}
              >
                <Button danger>停止</Button>
              </Popconfirm>
            )}
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {!env?.ready
              ? '请先按上方指引安装 VideoCaptioner'
              : '转写一条几分钟的视频可能要几分钟，请耐心等待'}
          </Text>
        </Flex>
      </Card>

      {/* ---------- 进度 ---------- */}
      {job && (
        <Card
          title={
            <Space>
              <span>任务 #{job.id}</span>
              <Tag color={JOB_STATUS_META[job.status].color}>
                {JOB_STATUS_META[job.status].label}
              </Tag>
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
          <Descriptions column={{ xs: 1, sm: 2, lg: 4 }} style={{ marginTop: 8 }}>
            <Descriptions.Item label="视频进度">
              {job.completed_videos} / {job.total_videos} 条
            </Descriptions.Item>
            <Descriptions.Item label="已产出字幕">
              {job.subtitle_count} 份
            </Descriptions.Item>
            <Descriptions.Item label="失败">
              {job.failed_videos > 0 ? (
                <Text type="danger">{job.failed_videos} 条</Text>
              ) : (
                '0 条'
              )}
            </Descriptions.Item>
            <Descriptions.Item label="耗时">
              {job.started_at
                ? formatElapsed((Date.now() - new Date(job.started_at).getTime()) / 1000)
                : '—'}
            </Descriptions.Item>
          </Descriptions>

          {job.current_video && running && (
            <Alert
              type="info"
              showIcon
              style={{ marginTop: 8 }}
              message={
                <span>
                  正在转写第 {job.current_index}/{job.total_videos} 条：
                  <Text strong>{job.current_video}</Text>
                  {job.current_elapsed_seconds > 0 &&
                    ` · 已用 ${formatElapsed(job.current_elapsed_seconds)}`}
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

          {/* 每条视频的处理明细 */}
          {job.items.length > 0 && (
            <Table
              style={{ marginTop: 12 }}
              rowKey="id"
              pagination={false}
              dataSource={job.items}
              columns={itemColumns(job, (item) =>
                item.subtitle_exists ? void openPreview(job.id, item.index) : undefined,
              )}
            />
          )}
        </Card>
      )}

      {/* ---------- 字幕产物清单 ---------- */}
      {subtitles.length > 0 && (
        <Card
          title={
            <Space size={8}>
              <FileTextOutlined style={{ color: 'var(--color-primary)' }} />
              <span>字幕产物</span>
              <Tag color="blue">{subtitles.length} 份</Tag>
            </Space>
          }
          style={{ marginBottom: 16 }}
        >
          <Table
            rowKey="index"
            pagination={false}
            dataSource={subtitles}
            columns={subtitleColumns((file) =>
              job ? void openPreview(job.id, file.index) : undefined,
            )}
          />
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
        extra={
          <Button onClick={() => void loadHistory()} loading={historyLoading}>
            刷新
          </Button>
        }
      >
        <Table
          rowKey="id"
          loading={historyLoading}
          dataSource={history}
          pagination={false}
          locale={{ emptyText: <Empty description="还没有任务记录" /> }}
          columns={historyColumns({
            onView: (id) => void openHistoryJob(id),
            onCancel: (id) => void cancel(id),
            onDelete: (id) => void remove(id),
          })}
        />
      </Card>

      {/* ---------- 字幕预览弹窗 ---------- */}
      <Modal
        open={preview !== null || previewLoading}
        title={
          preview && (
            <Space size={8}>
              <FileTextOutlined />
              <span>{preview.name}</span>
              <Text type="secondary" style={{ fontWeight: 'normal', fontSize: 12 }}>
                来自 {preview.source_name}
              </Text>
            </Space>
          )
        }
        footer={
          preview && (
            <Flex justify="space-between" align="center">
              <Text type="secondary" style={{ fontSize: 12 }}>
                {formatBytes(preview.size_bytes)}
                {preview.truncated && ' · 内容过长，只显示了开头一段'}
              </Text>
              <Button icon={<CopyOutlined />} onClick={() => preview && copyText(preview.content)}>
                复制全文
              </Button>
            </Flex>
          )
        }
        width={720}
        onCancel={() => setPreview(null)}
        destroyOnHidden
      >
        {previewLoading && !preview ? (
          <Flex justify="center" style={{ padding: 32 }}>
            <Spin />
          </Flex>
        ) : (
          preview && (
            <pre
              style={{
                maxHeight: '60vh',
                overflow: 'auto',
                padding: 12,
                background: 'var(--color-bg-soft, #f5f5f5)',
                borderRadius: 6,
                fontSize: 12,
                lineHeight: 1.7,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-all',
                margin: 0,
              }}
            >
              {preview.content}
            </pre>
          )
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

/** 每条视频的处理明细列定义 */
function itemColumns(
  job: SubtitleJob,
  onPreview: (item: SubtitleJobItem) => void,
): ColumnsType<SubtitleJobItem> {
  return [
    { title: '#', dataIndex: 'index', width: 48 },
    { title: '视频', dataIndex: 'source_name', ellipsis: true },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (status: keyof typeof ITEM_STATUS_META) => (
        <Tag color={ITEM_STATUS_META[status].color}>{ITEM_STATUS_META[status].label}</Tag>
      ),
    },
    {
      title: '字幕',
      width: 90,
      render: (_: unknown, record: SubtitleJobItem) =>
        record.subtitle_exists ? (
          <Text>{record.segment_count} 条</Text>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: '大小',
      width: 80,
      render: (_: unknown, record: SubtitleJobItem) =>
        record.file_size > 0 ? formatBytes(record.file_size) : '—',
    },
    {
      title: '耗时',
      dataIndex: 'elapsed_seconds',
      width: 80,
      render: (value: number, record: SubtitleJobItem) => {
        // 正在跑的这一条显示实时已用时
        if (record.status === 'running' && record.index === job.current_index) {
          return (
            <Text style={{ fontSize: 12 }}>
              {formatElapsed(job.current_elapsed_seconds)}
            </Text>
          )
        }
        return value > 0 ? `${value}s` : '—'
      },
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
    {
      title: '操作',
      width: 64,
      render: (_: unknown, record: SubtitleJobItem) =>
        record.subtitle_exists ? (
          <Button type="link" style={{ padding: 0 }} onClick={() => onPreview(record)}>
            预览
          </Button>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
  ]
}

/** 字幕产物清单列定义 */
function subtitleColumns(onPreview: (file: SubtitleFile) => void): ColumnsType<SubtitleFile> {
  return [
    { title: '#', dataIndex: 'index', width: 48 },
    { title: '文件名', dataIndex: 'name', ellipsis: true },
    { title: '来源视频', dataIndex: 'source_name', ellipsis: true },
    {
      title: '条数',
      dataIndex: 'segment_count',
      width: 72,
    },
    {
      title: '大小',
      dataIndex: 'size_bytes',
      width: 88,
      render: (value: number) => formatBytes(value),
    },
    {
      title: '操作',
      width: 64,
      render: (_: unknown, record: SubtitleFile) => (
        <Button type="link" style={{ padding: 0 }} onClick={() => onPreview(record)}>
          预览
        </Button>
      ),
    },
  ]
}

/** 历史任务表格列定义 */
function historyColumns(handlers: {
  onView: (jobId: number) => void
  onCancel: (jobId: number) => void
  onDelete: (jobId: number) => void
}): ColumnsType<SubtitleJob> {
  return [
    { title: 'ID', dataIndex: 'id', width: 64 },
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
    { title: '字幕', dataIndex: 'subtitle_count', width: 64 },
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
      width: 180,
      render: (_: unknown, record) => (
        <Space size={4}>
          <Button type="link" style={{ padding: 0 }} onClick={() => handlers.onView(record.id)}>
            查看
          </Button>
          {!isTerminalStatus(record.status) ? (
            <Button type="link" style={{ padding: 0 }} onClick={() => handlers.onCancel(record.id)}>
              取消
            </Button>
          ) : (
            <Popconfirm
              title="删除这条任务记录？"
              description="只删记录，已生成的字幕文件会保留在磁盘上。"
              okText="删除"
              cancelText="取消"
              onConfirm={() => handlers.onDelete(record.id)}
            >
              <Button type="link" danger style={{ padding: 0 }}>
                删除
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]
}
