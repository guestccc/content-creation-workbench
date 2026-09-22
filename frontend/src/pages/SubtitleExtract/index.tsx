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
 *
 * 字幕预览弹窗提供两种视图：`srt 原文` 与 `无时间文本`（去掉序号与时间轴）。
 * 后者是拿已返回的内容现算的展示变换，不另发请求，切换是瞬时的。
 *
 * 页面自己不写状态机：环境探测与手动指定目录在本文件夹的 useSubtitleEnv 里
 * （单页私有，不进公共 hooks），目录扫描、任务生命周期、历史列表分别在公共的
 * useSourceDir / useJobRunner / useJobList 里，这里只做编排与布局。
 */

import { CopyOutlined, FileTextOutlined, FolderOpenOutlined, ReloadOutlined } from '@ant-design/icons'
import { useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Flex,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType, TableProps } from 'antd/es/table'

import DirectoryPicker from '../../components/DirectoryPicker'
import HistoryCard from '../../components/HistoryCard'
import JobProgressCard, { JobTitle } from '../../components/JobProgressCard'
import JobRemarkModal from '../../components/JobRemarkModal'
import RetryAllButton from '../../components/RetryAllButton'
import SourceDirCard from '../../components/SourceDirCard'
import SubtitlePreviewModal from '../../components/SubtitlePreviewModal'
import type { SubtitlePreviewTarget } from '../../components/SubtitlePreviewModal'
import {
  jobActionsColumn,
  jobCreatedColumn,
  jobIdColumn,
  jobInputColumn,
  jobRemarkColumn,
  jobStatusColumn,
} from '../../components/jobColumns'
import {
  useApiMessage,
  useDirectoryPicker,
  useJobList,
  useJobPolling,
  useJobRemark,
  useJobRunner,
  usePurgeFiles,
  useSourceDir,
} from '../../hooks'
import type { UsePurgeFilesResult } from '../../hooks'
import {
  cancelSubtitleJob,
  createSubtitleJob,
  batchDeleteSubtitleJobs,
  deleteSubtitleJob,
  fetchSubtitleFiles,
  fetchSubtitleJob,
  fetchSubtitleJobs,
  retrySubtitleItem,
  retrySubtitleJob,
  updateSubtitleJobRemark,
} from '../../api/subtitle'
import { ITEM_STATUS_META, JOB_STATUS_META, isTerminalStatus } from '../../types/subtitle'
import type {
  SubtitleFile,
  SubtitleJob,
  SubtitleJobItem,
  SubtitleJobPayload,
} from '../../types/subtitle'
import { formatBytes, formatElapsed } from '../../utils/format'
import { useSubtitleEnv } from './useSubtitleEnv'

const { Text, Title, Paragraph } = Typography

/** 识别语言候选项：留空表示自动检测（后端默认值） */
const LANGUAGE_OPTIONS = [
  { value: '', label: '自动检测' },
  { value: 'zh', label: '中文' },
  { value: 'en', label: '英文' },
  { value: 'ja', label: '日文' },
  { value: 'ko', label: '韩文' },
]

/** 目录选择器弹窗的标题：同一个弹窗服务三种用途 */
const PICKER_TITLES = {
  input: '选择素材目录',
  output: '选择输出目录',
  vc: '选择 VideoCaptioner 目录',
} as const

/** VideoCaptioner 目录来源的展示标签 */
const VC_ROOT_SOURCE_LABEL: Record<string, string> = {
  environment: '环境变量',
  env_file: '.env 指定',
  auto: '自动探测',
}

export default function SubtitleExtract() {
  const { message, fail, contextHolder } = useApiMessage()
  const dir = useSourceDir(fail)
  const picker = useDirectoryPicker<'input' | 'output' | 'vc'>()

  // ---------- 输入与转写参数 ----------
  const [outputDir, setOutputDir] = useState('')
  const [asr, setAsr] = useState('bijian')
  const [language, setLanguage] = useState('')

  // ---------- 产物与预览弹窗 ----------
  const [subtitles, setSubtitles] = useState<SubtitleFile[]>([])
  /** 预览弹窗看的是哪一份字幕；null 表示关着（内容由弹窗自己拉） */
  const [previewTarget, setPreviewTarget] = useState<SubtitlePreviewTarget | null>(null)

  // ---------- 历史任务的下钻弹窗 ----------
  // 与镜头分割同一个模式：点「查看」开弹窗看这条任务，不顶掉页面上正在跑的那条
  /** 详情弹窗看的是哪条任务；null 表示关着 */
  const [detailJobId, setDetailJobId] = useState<number | null>(null)
  const [detailJob, setDetailJob] = useState<SubtitleJob | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)

  /** 关掉详情弹窗 */
  const closeJobDetail = () => {
    setDetailJobId(null)
    setDetailJob(null)
  }

  /** 拉取某条任务已产出的字幕清单 */
  const loadSubtitles = async (jobId: number) => {
    try {
      setSubtitles(await fetchSubtitleFiles(jobId))
    } catch {
      // 拉取失败不打扰用户，界面上会显示为空
    }
  }

  const history = useJobList<SubtitleJob>({ fetchList: fetchSubtitleJobs })

  // 历史表「备注」列的编辑开关：保存成功后就地刷新列表
  const remark = useJobRemark<SubtitleJob>({ message, onSaved: history.reload })

  // 删除时是否连产物一起清（每个删除确认框里都有这个勾选项）
  const purge = usePurgeFiles()

  const runner = useJobRunner<SubtitleJob, SubtitleJobPayload>({
    create: createSubtitleJob,
    cancel: cancelSubtitleJob,
    // take() 在这里调用：删除那一刻取值并重置，勾选只对这一次删除有效
    remove: (jobId) => deleteSubtitleJob(jobId, purge.take()),
    batchRemove: (ids) => batchDeleteSubtitleJobs(ids, purge.take()),
    fetchJob: fetchSubtitleJob,
    isTerminal: (job) => isTerminalStatus(job.status),
    fail,
    onChanged: history.reload,
    onFinished: (job) => void loadSubtitles(job.id),
    onRemoved: (jobId, wasCurrent) => {
      if (wasCurrent) {
        setSubtitles([])
      }
      if (detailJobId === jobId) {
        // 正在弹窗里看这条：记录没了就别让它挂在那儿
        closeJobDetail()
      }
    },
  })

  // 弹窗里那条任务还在跑时同样轮询：明细表里的状态才不是打开那一刻的快照。
  // 轮询由 useJobPolling 自己按「哪条任务、是否在跑」判断，终态一到就停。
  useJobPolling({
    job: detailJob,
    fetchJob: fetchSubtitleJob,
    isTerminal: (job) => isTerminalStatus(job.status),
    onUpdate: setDetailJob,
  })

  // 进页面探测环境（refresh(true) 绕过缓存，给「重新检测」按钮用），
  // 并把输入/输出默认停在素材目录的两个分段：materials/source → materials/subtitle。
  // 函数式更新 + 空值判断，避免覆盖用户在请求返回前已经手动选好的目录。
  const { env, loading: envLoading, refresh: refreshEnv, setVcRoot } = useSubtitleEnv({
    api: { message, fail, contextHolder },
    onLoaded: (loaded) => {
      if (loaded.default_input_dir) {
        dir.setPath((current) => current || loaded.default_input_dir)
      }
      if (loaded.default_output_dir) {
        setOutputDir((current) => current || loaded.default_output_dir)
      }
    },
  })

  const job = runner.job

  /** 当前看的是不是素材目录的 source 分段 —— 空列表时的提示文案要分情况 */
  const isEmptySourceDir =
    Boolean(env?.default_input_dir) && dir.data?.path === env?.default_input_dir

  /** 创建任务 */
  const start = async () => {
    if (!dir.path) {
      message.warning('请先选择素材目录')
      return
    }
    if (dir.videos.length === 0 && dir.selected.length === 0) {
      message.warning('该目录下没有可处理的视频文件')
      return
    }

    const created = await runner.submit({
      input_path: dir.path,
      recursive: dir.recursive,
      asr,
      ...(language ? { language } : {}),
      ...(outputDir ? { output_dir: outputDir } : {}),
      ...(dir.selected.length > 0 ? { files: dir.selected } : {}),
    })
    if (!created) {
      return
    }
    setSubtitles([])
    message.success('已开始提取字幕，可以在下面看进度')
  }

  /** 取消任务 */
  const cancel = async (jobId: number) => {
    if (await runner.cancel(jobId)) {
      message.info('已请求取消')
    }
  }

  /**
   * 重试接口返回的是**整条任务**的新状态，三处状态源都要对上号地刷新：
   * 页面上方那张当前任务卡、历史「查看」弹窗里那条、历史列表的行。
   */
  const applyRetried = (updated: SubtitleJob) => {
    runner.setJob((current) => (current?.id === updated.id ? updated : current))
    setDetailJob((current) => (current?.id === updated.id ? updated : current))
    history.reload()
  }

  /** 重试单条失败 / 跳过的视频：任务重新入队，其余视频的结果不动 */
  const retryItem = async (target: SubtitleJob, item: SubtitleJobItem) => {
    try {
      applyRetried(await retrySubtitleItem(target.id, item.index))
      message.success(`已重新入队：${item.source_name}`)
    } catch (error) {
      fail(error, '重试失败')
    }
  }

  /**
   * 重试全部未完成的视频。
   *
   * 走一条批量接口而不是循环调单条：第一次调用就把任务置回 pending，循环里
   * 第二次会撞上「任务尚未结束」的 409 而半途而废。
   */
  const retryAll = async (target: SubtitleJob) => {
    const count = target.failed_videos + target.skipped_videos
    try {
      applyRetried(await retrySubtitleJob(target.id))
      message.success(`已重新入队 ${count} 条`)
    } catch (error) {
      fail(error, '重试失败')
    }
  }

  /** 删除任务记录（是否连字幕文件一起删由确认框里的勾选决定） */
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
  const historyRowSelection: TableProps<SubtitleJob>['rowSelection'] = {
    selectedRowKeys: history.selectedRowKeys,
    onChange: (keys) => history.setSelectedRowKeys(keys.map(Number)),
    getCheckboxProps: (job) => ({ disabled: !isTerminalStatus(job.status) }),
  }

  /** 打开字幕预览（内容与视图由弹窗自己管，这里只交出「看哪一份」） */
  const openPreview = (jobId: number, index: number) => {
    setPreviewTarget({ jobId, index })
  }

  /**
   * 历史列表点「查看」：弹窗展示这条任务转了哪些视频。
   *
   * 刻意不动页面上的 `job` 与字幕清单 —— 那套进度卡和产物区属于「我正在跑的
   * 那条任务」，翻旧账不该把它顶掉；弹窗关掉，页面还是原样。
   */
  const openJobDetail = async (jobId: number) => {
    setDetailJobId(jobId)
    setDetailJob(null)
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

  /** 复制到剪贴板 */
  const copyText = (text: string) => {
    void navigator.clipboard.writeText(text).then(
      () => message.success('已复制'),
      () => message.error('复制失败，请手动选择复制'),
    )
  }

  const canSubmit = Boolean(env?.ready) && !runner.running && !runner.submitting

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
              <Space size={8}>
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={envLoading}
                  onClick={() => void refreshEnv(true)}
                >
                  重新检测
                </Button>
                <Button
                  size="small"
                  icon={<FolderOpenOutlined />}
                  onClick={() => picker.open('vc')}
                >
                  手动指定 VideoCaptioner 目录
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
            <Space size={8}>
              <span>环境正常</span>
              {env.version && <Tag color="green">VideoCaptioner {env.version}</Tag>}
              <Text type="secondary" style={{ fontSize: 12 }}>
                配置：{env.config_file}
              </Text>
            </Space>
          }
          description={
            <Space size={8} wrap>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {env.root
                  ? `目录：${env.root}（${VC_ROOT_SOURCE_LABEL[env.vc_root_source] ?? env.vc_root_source}）`
                  : 'VideoCaptioner 由后端解释器 / PATH 提供'}
              </Text>
              <Button
                size="small"
                type="link"
                style={{ padding: 0, fontSize: 12 }}
                onClick={() => picker.open('vc')}
              >
                更改目录
              </Button>
              {env.vc_root_source !== 'auto' && (
                <Button
                  size="small"
                  type="link"
                  style={{ padding: 0, fontSize: 12 }}
                  onClick={() => void setVcRoot('')}
                >
                  恢复自动探测
                </Button>
              )}
            </Space>
          }
        />
      )}

      {/* 环境可用但需要注意的情况（如 .env 里的指定会被同名环境变量覆盖） */}
      {env && env.warnings.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={
            <Space direction="vertical" size={4}>
              {env.warnings.map((text) => (
                <span key={text}>{text}</span>
              ))}
            </Space>
          }
        />
      )}

      {/* ---------- 输入与参数 ---------- */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} lg={14}>
          <SourceDirCard
            dir={dir}
            outputDir={outputDir}
            outputLabel="输出目录（字幕文件放这里）"
            outputPlaceholder="默认（materials/subtitle）"
            outputHint="每条视频生成一个同名 .srt 文件，重名自动加 -2、-3 后缀"
            isEmptySourceDir={isEmptySourceDir}
            onPickInput={() => picker.open('input')}
            onPickOutput={() => picker.open('output')}
          />
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
              loading={runner.submitting}
              disabled={!canSubmit}
            >
              开始提取
            </Button>
            {runner.running && job && (
              <Popconfirm
                title="取消当前任务？"
                description="已生成的字幕会保留，未处理的视频会被跳过。"
                okText="取消任务"
                cancelText="继续跑"
                onConfirm={() => void cancel(job.id)}
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
        <JobProgressCard
          title={<JobTitle jobId={job.id} meta={JOB_STATUS_META[job.status]} />}
          status={job.status}
          percent={job.progress_percent}
          running={runner.running}
          stats={[
            {
              label: '视频进度',
              value: `${job.completed_videos} / ${job.total_videos} 条`,
            },
            { label: '已产出字幕', value: `${job.subtitle_count} 份` },
            {
              label: '失败',
              value:
                job.failed_videos > 0 ? (
                  <Text type="danger">{job.failed_videos} 条</Text>
                ) : (
                  '0 条'
                ),
            },
            {
              label: '耗时',
              value: job.started_at
                ? formatElapsed((Date.now() - new Date(job.started_at).getTime()) / 1000)
                : '—',
            },
          ]}
          current={
            job.current_video
              ? {
                  label: '转写',
                  index: job.current_index,
                  total: job.total_videos,
                  name: job.current_video,
                  extra:
                    job.current_elapsed_seconds > 0
                      ? `已用 ${formatElapsed(job.current_elapsed_seconds)}`
                      : undefined,
                }
              : null
          }
          errorMessage={job.error_message}
          outputDir={job.output_dir}
        >
          {/* 每条视频的处理明细 */}
          {job.items.length > 0 && (
            <>
              {isTerminalStatus(job.status) && (
                <Flex justify="flex-end" style={{ marginTop: 12 }}>
                  <RetryAllButton
                    count={job.failed_videos + job.skipped_videos}
                    onRetry={() => void retryAll(job)}
                  />
                </Flex>
              )}
              <Table
                rowKey="id"
                pagination={false}
                dataSource={job.items}
                columns={itemColumns(
                  job,
                  (item) => {
                    if (item.subtitle_exists) {
                      void openPreview(job.id, item.index)
                    }
                  },
                  (item) => void retryItem(job, item),
                )}
              />
            </>
          )}
        </JobProgressCard>
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
            columns={subtitleColumns((file) => {
              if (job) {
                void openPreview(job.id, file.index)
              }
            })}
          />
        </Card>
      )}

      {/* ---------- 历史任务 ---------- */}
      <HistoryCard
        columns={historyColumns({
          onView: (id) => void openJobDetail(id),
          onCancel: (id) => void cancel(id),
          onDelete: (id) => void remove(id),
          onEditRemark: remark.open,
          onRetry: (target) => void retryAll(target),
          purge,
        })}
        dataSource={history.items}
        loading={history.loading}
        onRefresh={history.reload}
        rowSelection={historyRowSelection}
        pagination={{
          total: history.total,
          pageSize: 10,
          showSizeChanger: false,
          current: history.page,
          onChange: history.setPage,
        }}
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

      {/* ---------- 历史任务详情：这条任务转了哪些视频 ---------- */}
      <Modal
        open={detailJobId !== null}
        title={
          <Space size={8}>
            <span>字幕提取任务 #{detailJobId}</span>
            {detailJob && (
              <Tag color={JOB_STATUS_META[detailJob.status].color}>
                {JOB_STATUS_META[detailJob.status].label}
              </Tag>
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
              <DescriptionsBlock job={detailJob} />

              {detailJob.error_message && (
                <Alert
                  type="warning"
                  showIcon
                  message="部分内容未完成"
                  description={<Text style={{ fontSize: 12 }}>{detailJob.error_message}</Text>}
                />
              )}

              {/* 还在跑的时候这个弹窗自己会轮询，状态列是活的。
                  预览复用页面级的字幕文本弹窗，直接叠在这层上面 */}
              {isTerminalStatus(detailJob.status) && (
                <Flex justify="flex-end">
                  <RetryAllButton
                    count={detailJob.failed_videos + detailJob.skipped_videos}
                    onRetry={() => void retryAll(detailJob)}
                  />
                </Flex>
              )}
              <Table
                rowKey="id"
                pagination={false}
                dataSource={detailJob.items}
                locale={{ emptyText: <Empty description="这条任务没有视频明细" /> }}
                columns={itemColumns(
                  detailJob,
                  (item) => void openPreview(detailJob.id, item.index),
                  (item) => void retryItem(detailJob, item),
                )}
              />
            </Space>
          )
        )}
      </Modal>

      {/* ---------- 字幕预览弹窗（与一键成品选素材共用） ---------- */}
      <SubtitlePreviewModal
        target={previewTarget}
        onClose={() => setPreviewTarget(null)}
      />

      {/* ---------- 目录选择器 ---------- */}
      <DirectoryPicker
        open={picker.active !== null}
        title={PICKER_TITLES[picker.active ?? 'input']}
        initialPath={
          picker.active === 'vc'
            ? env?.vc_search_dir || env?.root || undefined
            : picker.active === 'output'
              ? outputDir || dir.path
              : dir.path
        }
        onClose={picker.close}
        onSelect={(path) => {
          if (picker.active === 'vc') {
            void setVcRoot(path)
          } else if (picker.active === 'output') {
            setOutputDir(path)
          } else {
            dir.setPath(path)
          }
        }}
      />

      {/* ---------- 备注编辑 ---------- */}
      {remark.editing && (
        <JobRemarkModal
          job={remark.editing}
          save={updateSubtitleJobRemark}
          onClose={remark.close}
          onSaved={remark.handleSaved}
        />
      )}
    </div>
  )
}

/** 任务详情弹窗里的概要信息 */
function DescriptionsBlock({ job }: { job: SubtitleJob }) {
  return (
    <Descriptions column={{ xs: 1, sm: 2 }}>
      <Descriptions.Item label="输入">{job.input_path}</Descriptions.Item>
      {job.output_dir && <Descriptions.Item label="输出">{job.output_dir}</Descriptions.Item>}
      <Descriptions.Item label="视频">
        {job.completed_videos} / {job.total_videos} 条完成
        {job.failed_videos > 0 && ` · ${job.failed_videos} 条失败`}
      </Descriptions.Item>
      <Descriptions.Item label="字幕">{job.subtitle_count} 份</Descriptions.Item>
    </Descriptions>
  )
}

/** 每条视频的处理明细列定义 */
function itemColumns(
  job: SubtitleJob,
  onPreview: (item: SubtitleJobItem) => void,
  onRetry?: (item: SubtitleJobItem) => void,
): ColumnsType<SubtitleJobItem> {
  const columns: ColumnsType<SubtitleJobItem> = [
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
            <Text style={{ fontSize: 12 }}>{formatElapsed(job.current_elapsed_seconds)}</Text>
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
      width: 72,
      render: (_: unknown, record: SubtitleJobItem) => {
        // 有字幕就预览；没有字幕且任务已跑完，才谈得上重试 —— 两者互斥，
        // 一个条目不会既产出了字幕又等着被补跑
        if (record.subtitle_exists) {
          return (
            <Button type="link" style={{ padding: 0 }} onClick={() => onPreview(record)}>
              预览
            </Button>
          )
        }
        // 可重试 = 「没有产出」：failed 是跑了但失败，skipped 是被取消 /
        // 服务重启时压根没轮到
        const retryable =
          onRetry !== undefined &&
          (record.status === 'failed' || record.status === 'skipped') &&
          isTerminalStatus(job.status)
        if (retryable) {
          return (
            <Button type="link" style={{ padding: 0 }} onClick={() => onRetry?.(record)}>
              重试
            </Button>
          )
        }
        return <Text type="secondary">—</Text>
      },
    },
  ]

  return columns
}

/** 字幕产物清单列定义 */
function subtitleColumns(onPreview: (file: SubtitleFile) => void): ColumnsType<SubtitleFile> {
  return [
    { title: '#', dataIndex: 'index', width: 48 },
    { title: '文件名', dataIndex: 'name', ellipsis: true },
    { title: '来源视频', dataIndex: 'source_name', ellipsis: true },
    { title: '条数', dataIndex: 'segment_count', width: 72 },
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
  onEditRemark: (job: SubtitleJob) => void
  onRetry: (job: SubtitleJob) => void
  purge: UsePurgeFilesResult
}): ColumnsType<SubtitleJob> {
  return [
    jobIdColumn<SubtitleJob>(),
    jobStatusColumn<SubtitleJob>(JOB_STATUS_META),
    jobInputColumn<SubtitleJob>(),
    { title: '视频', dataIndex: 'total_videos', width: 64 },
    { title: '字幕', dataIndex: 'subtitle_count', width: 64 },
    jobRemarkColumn<SubtitleJob>({ onEdit: handlers.onEditRemark }),
    jobCreatedColumn<SubtitleJob>(),
    jobActionsColumn<SubtitleJob>({
      isTerminal: (job) => isTerminalStatus(job.status),
      onView: handlers.onView,
      onCancel: handlers.onCancel,
      onDelete: handlers.onDelete,
      deleteDescription: '删除后不可恢复。',
      purge: handlers.purge,
      // 列表接口不带逐条明细，只能靠行上的计数判断还有没有没产出的
      onRetry: handlers.onRetry,
      canRetry: (job) => job.failed_videos + job.skipped_videos > 0,
    }),
  ]
}
