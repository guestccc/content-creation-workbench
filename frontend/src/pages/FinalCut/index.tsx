/**
 * 一键成品页面。
 *
 * 流程：① 选素材（字幕 + 成片，历史产物或本地文件）→ ② AI 文案
 * （拆解字幕素材、按视频时长定量生成候选文案，勾选/改字）→
 * ③ 框选与合成（在视频预览上框位置、选样式，一条文案一个成片）。
 *
 * 页面只做编排：环境探测在 useFinalcutEnv，三步状态机在 useFinalcutFlow
 * （均本页私有）；历史列表用公共的 useJobList，目录/文件选择复用
 * DirectoryPicker。
 */

import { useMemo, useState } from 'react'
import {
  ApiOutlined,
  ArrowLeftOutlined,
  ClearOutlined,
  HistoryOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  Card,
  Empty,
  Flex,
  List,
  Modal,
  Popconfirm,
  Space,
  Spin,
  Steps,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'

import DirectoryPicker from '../../components/DirectoryPicker'
import HistoryCard from '../../components/HistoryCard'
import JobProgressCard from '../../components/JobProgressCard'
import JobRemarkModal from '../../components/JobRemarkModal'
import VideoPreviewModal from '../../components/VideoPreviewModal'
import {
  jobActionsColumn,
  jobCreatedColumn,
  jobIdColumn,
  jobRemarkColumn,
  jobStatusColumn,
} from '../../components/jobColumns'
import {
  useApiMessage,
  useDirectoryPicker,
  useJobList,
  useJobRemark,
  usePurgeFiles,
} from '../../hooks'
import { formatBytes, formatDuration } from '../../utils/format'
import {
  fetchCopyJobs,
  fetchRenderJobs,
  updateCopyJobRemark,
  updateRenderJobRemark,
} from '../../api/finalcut'
import { localVideoPreviewUrl } from '../../api/filesystem'
import {
  COPY_PHASE_LABEL,
  ITEM_STATUS_META,
  JOB_STATUS_META,
  isTerminalStatus,
} from '../../types/finalcut'
import type {
  FinalcutCopyJob,
  FinalcutRenderJob,
  FinalcutSource,
} from '../../types/finalcut'
import AiSettingsModal from './AiSettingsModal'
import CopyCard from './CopyCard'
import RenderStep from './RenderStep'
import { useFinalcutEnv } from './useFinalcutEnv'
import {
  localFileToMaterial,
  sourceToMaterial,
  useFinalcutFlow,
} from './useFinalcutFlow'
import type { SelectedMaterial } from './useFinalcutFlow'

const { Text } = Typography

/** 字幕文件 / 视频文件的后缀白名单（与后端 schemas/finalcut_job.py 一致） */
const SUBTITLE_EXTENSIONS = ['.srt', '.ass', '.vtt']
const VIDEO_EXTENSIONS = ['.mp4', '.mov', '.mkv', '.webm']

/** 素材卡标题旁的来源标签 */
function OriginTag({ origin }: { origin: SelectedMaterial['origin'] }) {
  return origin === 'history' ? <Tag color="blue">历史产物</Tag> : <Tag color="purple">本地文件</Tag>
}

/** 文案任务历史表的列（文案任务磁盘上无产物，删除确认不带 purge 勾选） */
function copyJobColumns(
  flow: ReturnType<typeof useFinalcutFlow>,
  onEditRemark: (job: FinalcutCopyJob) => void,
): ColumnsType<FinalcutCopyJob> {
  return [
    jobIdColumn(),
    jobStatusColumn(JOB_STATUS_META),
    {
      title: '字幕素材',
      dataIndex: 'subtitle_path',
      ellipsis: true,
      render: (value: string) => {
        const name = value.split(/[\\/]/).pop() || value
        return (
          <Tooltip title={value}>
            <Text>{name}</Text>
          </Tooltip>
        )
      },
    },
    { title: '条数', dataIndex: 'copy_count', width: 64 },
    jobRemarkColumn<FinalcutCopyJob>({ onEdit: onEditRemark }),
    {
      title: '结果',
      key: 'copies',
      width: 90,
      render: (_, record) =>
        record.status === 'success' ? `${record.result?.copies.length ?? 0} 条候选` : '—',
    },
    jobCreatedColumn(),
    {
      title: '操作',
      key: 'actions',
      width: 180,
      render: (_, record) => (
        <Space size={4}>
          <Button type="link" style={{ padding: 0 }} onClick={() => void flow.openCopyJob(record.id)}>
            查看
          </Button>
          {!isTerminalStatus(record.status) ? (
            <Button
              type="link"
              style={{ padding: 0 }}
              onClick={() => void flow.cancelCopyJobById(record.id)}
            >
              取消
            </Button>
          ) : (
            <Popconfirm
              title="删除这条任务记录？"
              description="文案任务在磁盘上没有产物，删除只清掉这条记录。"
              okText="删除"
              cancelText="取消"
              onConfirm={() => void flow.removeCopyJob(record.id)}
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

/** 合成任务历史表的列（有磁盘产物，删除确认带 purge 勾选） */
function renderJobColumns(
  flow: ReturnType<typeof useFinalcutFlow>,
  purge: ReturnType<typeof usePurgeFiles>,
  onView: (jobId: number) => void,
  onEditRemark: (job: FinalcutRenderJob) => void,
): ColumnsType<FinalcutRenderJob> {
  return [
    jobIdColumn(),
    jobStatusColumn(JOB_STATUS_META),
    {
      title: '成片',
      key: 'items',
      width: 90,
      render: (_, record) =>
        `${record.completed_items}/${record.total_items}`,
    },
    {
      title: '输出目录',
      dataIndex: 'output_dir',
      ellipsis: true,
      render: (value: string) => {
        const name = value.split(/[\\/]/).pop() || value
        return (
          <Tooltip title={value}>
            <Text style={{ fontSize: 12 }}>{name}</Text>
          </Tooltip>
        )
      },
    },
    jobRemarkColumn<FinalcutRenderJob>({ onEdit: onEditRemark }),
    jobCreatedColumn(),
    jobActionsColumn({
      isTerminal: (record) => isTerminalStatus(record.status),
      onView,
      onCancel: (jobId) => void flow.cancelRenderJobById(jobId),
      onDelete: (jobId) => void flow.removeRenderJob(jobId),
      deleteDescription: '合成任务的产物是输出目录里的成片视频。',
      purge,
    }),
  ]
}

export default function FinalCut() {  const api = useApiMessage()
  const purge = usePurgeFiles()
  const copyHistory = useJobList<FinalcutCopyJob>({ fetchList: fetchCopyJobs })
  const renderHistory = useJobList<FinalcutRenderJob>({ fetchList: fetchRenderJobs })

  // 两个历史表各有自己的「备注」编辑开关：保存成功后就地刷新各自的列表
  const copyRemark = useJobRemark<FinalcutCopyJob>({
    message: api.message,
    onSaved: copyHistory.reload,
  })
  const renderRemark = useJobRemark<FinalcutRenderJob>({
    message: api.message,
    onSaved: renderHistory.reload,
  })
  const flow = useFinalcutFlow(api, {
    onCopyJobChanged: copyHistory.reload,
    onRenderJobChanged: renderHistory.reload,
    purge,
  })
  const { env, loading: envLoading, error: envError, refresh } = useFinalcutEnv({
    api,
    onLoaded: (data) => flow.fillDefaultOutputDir(data.default_output_dir),
  })

  // 弹窗开关：「历史产物」与「本地文件」各一对（字幕/成片），同一时刻只开一个
  const picker = useDirectoryPicker<'subtitleFile' | 'videoFile'>()
  const [sourcesOpen, setSourcesOpen] = useState<'subtitle' | 'video' | null>(null)
  const [aiSettingsOpen, setAiSettingsOpen] = useState(false)
  const [previewOpen, setPreviewOpen] = useState(false)
  /** 合成历史「查看」打开的详情（不影响当前任务，用 read 而不是 setJob） */
  const [renderDetail, setRenderDetail] = useState<FinalcutRenderJob | null>(null)

  const openRenderDetail = (jobId: number) => {
    void flow.readRenderJob(jobId).then((job) => {
      if (job) {
        setRenderDetail(job)
      }
    })
  }

  const openSources = (kind: 'subtitle' | 'video') => {
    setSourcesOpen(kind)
    void flow.loadSources()
  }

  /** 视频预览地址：历史产物走混剪的成片流，本地文件走公共的只读预览 */
  const previewUrl = useMemo(() => {
    if (!flow.video) {
      return ''
    }
    if (flow.video.origin === 'local') {
      return localVideoPreviewUrl(flow.video.path)
    }
    const hit = flow.sources?.videos.find((item) => item.path === flow.video?.path)
    return hit?.video_url || localVideoPreviewUrl(flow.video.path)
  }, [flow.video, flow.sources])

  const sourceItems: FinalcutSource[] =
    sourcesOpen === 'subtitle' ? (flow.sources?.subtitles ?? []) : (flow.sources?.videos ?? [])

  const pickSource = (source: FinalcutSource) => {
    if (sourcesOpen === 'subtitle') {
      flow.selectSubtitle(sourceToMaterial(source))
    } else {
      flow.selectVideo(sourceToMaterial(source))
    }
    setSourcesOpen(null)
  }

  const copyJob = flow.copyJob
  const copyResult = copyJob?.status === 'success' ? copyJob.result : null

  return (
    <Flex vertical gap={16}>
      {api.contextHolder}

      {envError && (
        <Alert type="error" showIcon message="环境探测失败" description={envError} />
      )}
      {env && !env.ready && (
        <Alert
          type="warning"
          showIcon
          message="一键成品的环境还没就绪"
          description={
            <Flex vertical gap={4}>
              {!env.ai.ok && <Text>· AI：{env.ai.detail}。{env.ai.fix_hint}</Text>}
              {!env.ffmpeg.ok && (
                <Text>· ffmpeg：{env.ffmpeg.detail}。{env.ffmpeg.fix_hint}</Text>
              )}
              {!env.font && <Text>· 中文字体：未探测到（烧进画面的文案会变成方块）</Text>}
              {env.warnings.map((text) => (
                <Text key={text} type="secondary">· {text}</Text>
              ))}
            </Flex>
          }
          action={
            <Space direction="vertical">
              <Button
                size="small"
                icon={<ApiOutlined />}
                onClick={() => setAiSettingsOpen(true)}
              >
                AI 配置
              </Button>
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={envLoading}
                onClick={() => void refresh(true)}
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
          message={`环境就绪：${env.ai.model} · ffmpeg ${env.ffmpeg.version} · ${env.font?.family ?? ''}`}
          action={
            <Button size="small" icon={<ApiOutlined />} onClick={() => setAiSettingsOpen(true)}>
              AI 配置
            </Button>
          }
        />
      )}

      <Steps
        current={flow.step}
        items={[
          { title: '选择素材', description: '字幕 + 成片视频' },
          { title: 'AI 文案', description: '拆解素材，生成候选文案' },
          { title: '框选与合成', description: '框位置、选样式、出成片' },
        ]}
      />

      {/* ---------------- 第 ① 步：选素材 ---------------- */}
      {flow.step === 0 && (
        <>
          <Card title="字幕素材">
            {flow.subtitle ? (
              <Flex align="center" gap={8} wrap>
                <OriginTag origin={flow.subtitle.origin} />
                <Text strong>{flow.subtitle.name}</Text>
                {flow.subtitle.size_bytes > 0 && (
                  <Text type="secondary">{formatBytes(flow.subtitle.size_bytes)}</Text>
                )}
                <Text code ellipsis={{ tooltip: flow.subtitle.path }} style={{ maxWidth: 420 }}>
                  {flow.subtitle.path}
                </Text>
                <Button
                  type="text"
                  size="small"
                  icon={<ClearOutlined />}
                  onClick={flow.clearSubtitle}
                >
                  重选
                </Button>
              </Flex>
            ) : (
              <Empty description="还没有选择字幕" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            )}
            <Space style={{ marginTop: 12 }}>
              <Button icon={<HistoryOutlined />} onClick={() => openSources('subtitle')}>
                从历史产物选择
              </Button>
              <Button onClick={() => picker.open('subtitleFile')}>选择本地文件</Button>
            </Space>
          </Card>

          <Card title="成片视频">
            {flow.video ? (
              <Flex align="center" gap={8} wrap>
                <OriginTag origin={flow.video.origin} />
                <Text strong>{flow.video.name}</Text>
                {flow.video.duration_seconds != null && flow.video.duration_seconds > 0 && (
                  <Text type="secondary">{formatDuration(flow.video.duration_seconds)}</Text>
                )}
                <Text code ellipsis={{ tooltip: flow.video.path }} style={{ maxWidth: 420 }}>
                  {flow.video.path}
                </Text>
                <Button
                  type="text"
                  size="small"
                  icon={<PlayCircleOutlined />}
                  onClick={() => setPreviewOpen(true)}
                >
                  预览
                </Button>
                <Button type="text" size="small" icon={<ClearOutlined />} onClick={flow.clearVideo}>
                  重选
                </Button>
              </Flex>
            ) : (
              <Empty description="还没有选择成片视频" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            )}
            <Space style={{ marginTop: 12 }}>
              <Button icon={<HistoryOutlined />} onClick={() => openSources('video')}>
                从历史产物选择
              </Button>
              <Button onClick={() => picker.open('videoFile')}>选择本地文件</Button>
            </Space>
          </Card>

          <Flex justify="center">
            {/* disabled 的 Button 不冒泡鼠标事件，Tooltip 要挂在包裹元素上 */}
            <Tooltip title={flow.validationError || undefined}>
              <span>
                <Button
                  type="primary"
                  size="large"
                  icon={<ThunderboltOutlined />}
                  disabled={Boolean(flow.validationError) || flow.copyJobRunning}
                  loading={flow.copyJobSubmitting}
                  onClick={() => void flow.startCopyJob()}
                >
                  生成文案
                </Button>
              </span>
            </Tooltip>
          </Flex>
        </>
      )}

      {/* ---------------- 第 ② 步：AI 文案 ---------------- */}
      {flow.step === 1 && (
        <>
          <Flex justify="space-between" align="center">
            <Button icon={<ArrowLeftOutlined />} onClick={flow.goBack}>
              返回重选素材
            </Button>
            <Button
              icon={<ReloadOutlined />}
              disabled={flow.copyJobRunning}
              loading={flow.copyJobSubmitting}
              onClick={() => void flow.regenerate()}
            >
              换一批
            </Button>
          </Flex>

          {copyJob && !isTerminalStatus(copyJob.status) && (
            <JobProgressCard
              title="AI 正在拆解字幕素材并生成文案"
              status={copyJob.status}
              percent={copyJob.progress_percent}
              running
              stats={[
                { label: '模型', value: copyJob.model || '—' },
                { label: '候选条数', value: copyJob.copy_count },
                { label: '视频时长', value: formatDuration(copyJob.video_duration) },
              ]}
              current={
                copyJob.current_phase
                  ? {
                      label: '阶段',
                      index: ['read', 'analyze', 'parse'].indexOf(copyJob.current_phase) + 1,
                      total: 3,
                      name: COPY_PHASE_LABEL[copyJob.current_phase],
                    }
                  : null
              }
            />
          )}
          {copyJob?.status === 'failed' && (
            <Alert
              type="error"
              showIcon
              message="文案生成失败"
              description={copyJob.error_message}
            />
          )}
          {copyJob?.status === 'cancelled' && (
            <Alert type="info" showIcon message="任务已取消，生成的结果未保留" />
          )}

          {copyResult && (
            <>
              <Card title="字幕素材拆解">
                <Flex vertical gap={4}>
                  <Text>主题：{copyResult.analysis.topic || '—'}</Text>
                  <Text>目标受众：{copyResult.analysis.audience || '—'}</Text>
                  <Text>
                    核心卖点：
                    {copyResult.analysis.selling_points.length > 0
                      ? copyResult.analysis.selling_points.map((point) => (
                          <Tag key={point} color="orange">{point}</Tag>
                        ))
                      : '—'}
                  </Text>
                  <Text>调性：{copyResult.analysis.tone || '—'}</Text>
                </Flex>
              </Card>

              <Flex vertical gap={12}>
                {flow.candidates.map((state) => (
                  <CopyCard
                    key={state.key}
                    candidate={copyResult.copies[state.key]}
                    state={state}
                    onToggle={() => flow.toggleCandidate(state.key)}
                    onTextChange={(text) => flow.updateCandidateText(state.key, text)}
                  />
                ))}
              </Flex>

              <Flex justify="center" gap={12} align="center">
                <Text type="secondary">已勾选 {flow.checkedCount} 条</Text>
                <Button
                  type="primary"
                  size="large"
                  disabled={flow.checkedCount === 0}
                  onClick={flow.goToBoxStep}
                >
                  进入下一步：框选与合成
                </Button>
              </Flex>
            </>
          )}
        </>
      )}

      {/* ---------------- 第 ③ 步：框选与合成 ---------------- */}
      {flow.step === 2 && (
        <>
          <RenderStep
            flow={flow}
            videoUrl={previewUrl}
            videoKey={flow.video?.path}
            styles={env?.text_styles}
          />

          <HistoryCard<FinalcutRenderJob>
            columns={renderJobColumns(flow, purge, openRenderDetail, renderRemark.open)}
            dataSource={renderHistory.items}
            loading={renderHistory.loading}
            onRefresh={renderHistory.reload}
            emptyText={<Empty description="还没有合成任务记录" />}
            pagination={{
              current: renderHistory.page,
              total: renderHistory.total,
              pageSize: 10,
              onChange: renderHistory.setPage,
            }}
            rowSelection={{
              selectedRowKeys: renderHistory.selectedRowKeys,
              onChange: (keys) => renderHistory.setSelectedRowKeys(keys as number[]),
            }}
            extra={
              <Popconfirm
                title={`删除选中的 ${renderHistory.selectedRowKeys.length} 条任务记录？`}
                description={
                  <div>
                    <div>删除后不可恢复。</div>
                    {purge.checkbox}
                  </div>
                }
                okText="删除"
                cancelText="取消"
                onConfirm={() => {
                  void flow
                    .removeRenderJobs(renderHistory.selectedRowKeys)
                    .then((ok) => ok && renderHistory.clearSelection())
                }}
                onOpenChange={(open) => {
                  if (open) {
                    purge.reset()
                  }
                }}
              >
                <Button danger size="small" disabled={renderHistory.selectedRowKeys.length === 0}>
                  批量删除
                </Button>
              </Popconfirm>
            }
          />
        </>
      )}

      {/* 文案历史任务（第 ③ 步展示合成历史，这里只在 ①② 步出现） */}
      {flow.step <= 1 && (
        <HistoryCard<FinalcutCopyJob>
          columns={copyJobColumns(flow, copyRemark.open)}
          dataSource={copyHistory.items}
          loading={copyHistory.loading}
          onRefresh={copyHistory.reload}
          emptyText={<Empty description="还没有文案任务记录" />}
          pagination={{
            current: copyHistory.page,
            total: copyHistory.total,
            pageSize: 10,
            onChange: copyHistory.setPage,
          }}
          rowSelection={{
            selectedRowKeys: copyHistory.selectedRowKeys,
            onChange: (keys) => copyHistory.setSelectedRowKeys(keys as number[]),
          }}
          extra={
            copyHistory.selectedRowKeys.length > 0 ? (
              <Popconfirm
                title={`删除选中的 ${copyHistory.selectedRowKeys.length} 条记录？`}
                description="文案任务在磁盘上没有产物，批量删除只清掉记录。"
                okText="删除"
                cancelText="取消"
                onConfirm={() => {
                  void flow
                    .removeCopyJobs(copyHistory.selectedRowKeys)
                    .then((ok) => ok && copyHistory.clearSelection())
                }}
              >
                <Button danger size="small">
                  批量删除
                </Button>
              </Popconfirm>
            ) : undefined
          }
        />
      )}

      {/* 历史产物选择弹窗（字幕 / 成片共用一个，列表内容按 kind 切换） */}
      <Modal
        open={sourcesOpen !== null}
        title={sourcesOpen === 'subtitle' ? '选择字幕产物' : '选择混剪成片'}
        footer={null}
        onCancel={() => setSourcesOpen(null)}
        destroyOnHidden
      >
        {flow.sourcesLoading ? (
          <div style={{ textAlign: 'center', padding: '40px 0' }}>
            <Spin />
          </div>
        ) : sourceItems.length === 0 ? (
          <Empty
            description={
              sourcesOpen === 'subtitle'
                ? '还没有字幕产物，先去「字幕提取」跑一个任务'
                : '还没有混剪成片，先去「智能混剪」跑一个任务'
            }
          />
        ) : (
          <List
            dataSource={sourceItems}
            renderItem={(item) => (
              <List.Item style={{ cursor: 'pointer' }} onClick={() => pickSource(item)}>
                <Flex vertical style={{ minWidth: 0 }}>
                  <Text>{item.name}</Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {item.duration_seconds != null && item.duration_seconds > 0
                      ? `${formatDuration(item.duration_seconds)} · `
                      : ''}
                    {formatBytes(item.size_bytes)}
                  </Text>
                </Flex>
              </List.Item>
            )}
          />
        )}
      </Modal>

      {/* 本地文件选择（DirectoryPicker 的文件模式：传了 fileExtensions 才列出文件） */}
      <DirectoryPicker
        open={picker.active === 'subtitleFile'}
        title="选择字幕文件"
        fileExtensions={SUBTITLE_EXTENSIONS}
        onSelectFile={(path) => flow.selectSubtitle(localFileToMaterial(path))}
        onSelect={() => undefined}
        onClose={picker.close}
      />
      <DirectoryPicker
        open={picker.active === 'videoFile'}
        title="选择成片视频"
        fileExtensions={VIDEO_EXTENSIONS}
        onSelectFile={(path) => flow.selectVideo(localFileToMaterial(path))}
        onSelect={() => undefined}
        onClose={picker.close}
      />

      <VideoPreviewModal
        open={previewOpen}
        title={flow.video?.name}
        src={previewUrl}
        videoKey={flow.video?.path}
        caption={flow.video?.path}
        onClose={() => setPreviewOpen(false)}
      />

      <AiSettingsModal
        open={aiSettingsOpen}
        api={api}
        onClose={() => setAiSettingsOpen(false)}
        onSaved={() => void refresh(true)}
      />

      {/* ---------- 备注编辑（两个历史表各一个，同一时刻只开一个） ---------- */}
      {copyRemark.editing && (
        <JobRemarkModal
          job={copyRemark.editing}
          save={updateCopyJobRemark}
          onClose={copyRemark.close}
          onSaved={copyRemark.handleSaved}
        />
      )}
      {renderRemark.editing && (
        <JobRemarkModal
          job={renderRemark.editing}
          save={updateRenderJobRemark}
          onClose={renderRemark.close}
          onSaved={renderRemark.handleSaved}
        />
      )}

      {/* 合成任务详情（历史「查看」）：逐条成片的状态与产物 */}
      <Modal
        open={renderDetail !== null}
        title={renderDetail ? `合成任务 #${renderDetail.id} 明细` : ''}
        footer={null}
        width={860}
        onCancel={() => setRenderDetail(null)}
        destroyOnHidden
      >
        {renderDetail && (
          <Flex vertical gap={8}>
            {renderDetail.error_message && (
              <Alert type="warning" showIcon message={renderDetail.error_message} />
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              输出目录：<Text code>{renderDetail.output_dir}</Text>
            </Text>
            <Table
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={renderDetail.items}
              columns={[
                { title: '#', dataIndex: 'index', width: 48 },
                {
                  title: '状态',
                  dataIndex: 'status',
                  width: 76,
                  render: (status: keyof typeof ITEM_STATUS_META) => (
                    <Tag color={ITEM_STATUS_META[status].color}>
                      {ITEM_STATUS_META[status].label}
                    </Tag>
                  ),
                },
                {
                  title: '文案',
                  dataIndex: 'copy_text',
                  ellipsis: true,
                  render: (value: string) => (
                    <Tooltip title={value}>
                      <Text style={{ fontSize: 12 }}>{value}</Text>
                    </Tooltip>
                  ),
                },
                { title: '样式', dataIndex: 'style', width: 90 },
                {
                  title: '时长/大小',
                  key: 'meta',
                  width: 130,
                  render: (_, item) =>
                    item.status === 'success'
                      ? `${formatDuration(item.duration_seconds)} · ${formatBytes(item.size_bytes)}`
                      : '—',
                },
                {
                  title: '说明',
                  key: 'error',
                  ellipsis: true,
                  render: (_, item) => (
                    <Text type={item.status === 'failed' ? 'danger' : 'secondary'} style={{ fontSize: 12 }}>
                      {item.error_message || '—'}
                    </Text>
                  ),
                },
              ]}
            />
          </Flex>
        )}
      </Modal>
    </Flex>
  )
}
