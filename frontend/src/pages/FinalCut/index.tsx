/**
 * 一键成品页面。
 *
 * 流程：① 选素材（字幕 + 成片，历史产物或本地文件）→ ② AI 文案
 * （拆解字幕素材、按视频时长与口播语速定量生成候选文案，改字、复制）。
 *
 * **到这里就结束了**：文案只有一份，既拿去剪映配音、也拿去人工烧字 —— 烧录
 * （原第 ③ 步的框选与合成）已从页面摘掉，后端接口保留。
 *
 * 页面只做编排：环境探测在 useFinalcutEnv，两步状态机在 useFinalcutFlow
 * （均本页私有）；历史列表用公共的 useJobList，目录/文件选择复用
 * DirectoryPicker。
 */

import { useMemo, useState } from 'react'
import {
  ApiOutlined,
  ArrowLeftOutlined,
  ClearOutlined,
  FileTextOutlined,
  HistoryOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Flex,
  InputNumber,
  List,
  Modal,
  Popconfirm,
  Row,
  Space,
  Spin,
  Steps,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'

import DirectoryPicker from '../../components/DirectoryPicker'
import HistoryCard from '../../components/HistoryCard'
import JobProgressCard from '../../components/JobProgressCard'
import JobRemarkModal from '../../components/JobRemarkModal'
import SubtitlePreviewModal from '../../components/SubtitlePreviewModal'
import type { SubtitlePreviewTarget } from '../../components/SubtitlePreviewModal'
import VideoPreviewModal from '../../components/VideoPreviewModal'
import {
  jobCreatedColumn,
  jobIdColumn,
  jobRemarkColumn,
  jobStatusColumn,
} from '../../components/jobColumns'
import { useApiMessage, useDirectoryPicker, useJobList, useJobRemark } from '../../hooks'
import { formatBytes, formatDuration } from '../../utils/format'
import { fetchCopyJobs, updateCopyJobRemark } from '../../api/finalcut'
import { localVideoPreviewUrl } from '../../api/filesystem'
import { COPY_PHASE_LABEL, JOB_STATUS_META, isTerminalStatus } from '../../types/finalcut'
import type { FinalcutCopyJob, FinalcutSource } from '../../types/finalcut'
import AiSettingsModal from '../../components/AiSettingsModal'
import CopyCard from './CopyCard'
import SubtitleMaterialCard from './SubtitleMaterialCard'
import { copyCharBudget } from './copyLength'
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

/**
 * 候选文案的 Tab 标签：序号 + 角度。
 *
 * 原先这里还有一个勾选框（勾哪几条进第 ③ 步烧录），烧录摘掉后没有「选中」
 * 这个概念了 —— 复制哪条由「正在看哪个 Tab」决定，标签上只剩识别信息。
 */
function CandidateTabLabel({ index, angle }: { index: number; angle: string }) {
  return (
    <Flex align="center" gap={6}>
      <span>文案 {index + 1}</span>
      {angle && (
        <Tag color="geekblue" style={{ marginInlineEnd: 0 }}>
          {angle}
        </Tag>
      )}
    </Flex>
  )
}

/**
 * 语速输入框（字/秒）：某条配音要念快 / 念慢就在这里改，值随任务落库。
 *
 * 空值 = 跟随全局默认（提交不带字段，后端按当前全局值快照进任务）。
 * 范围 1.0–15.0 与后端同一套；min/max 拦不住手输的越界值，提交前
 * useFinalcutFlow 还会本地拦一道。
 */
function RateInput({
  value,
  onChange,
}: {
  value: number | null
  onChange: (value: number | null) => void
}) {
  return (
    <Space size={6}>
      <Text type="secondary">口播语速（字/秒）</Text>
      <InputNumber
        min={1}
        max={15}
        step={0.1}
        value={value}
        onChange={onChange}
        placeholder="跟随默认"
        style={{ width: 88 }}
      />
    </Space>
  )
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
    {
      // 0 = 升级前的老任务（当时按全局值跑的，值不可考），如实显示「—」
      title: '语速',
      dataIndex: 'chars_per_second',
      width: 70,
      render: (value: number) => (value > 0 ? `${value} 字/秒` : '—'),
    },
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
      width: 220,
      render: (_, record) => (
        <Space size={4}>
          <Button type="link" style={{ padding: 0 }} onClick={() => void flow.openCopyJob(record.id)}>
            查看
          </Button>
          {/* 文案任务没有条目级状态，重试 = 按原参数另起一条新任务（新 id）。
              只有没跑出结果的才给点：跑成功的重跑一遍没有意义 */}
          {isTerminalStatus(record.status) &&
            (record.status === 'failed' || record.status === 'cancelled') && (
              <Button
                type="link"
                style={{ padding: 0 }}
                onClick={() => void flow.retryCopyJobById(record.id)}
              >
                重试
              </Button>
            )}
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

export default function FinalCut() {
  const api = useApiMessage()
  const copyHistory = useJobList<FinalcutCopyJob>({ fetchList: fetchCopyJobs })

  const copyRemark = useJobRemark<FinalcutCopyJob>({
    message: api.message,
    onSaved: copyHistory.reload,
  })
  const flow = useFinalcutFlow(api, { onCopyJobChanged: copyHistory.reload })
  const { env, loading: envLoading, error: envError, refresh } = useFinalcutEnv({ api })

  // 弹窗开关：「历史产物」与「本地文件」各一对（字幕/成片），同一时刻只开一个
  const picker = useDirectoryPicker<'subtitleFile' | 'videoFile'>()
  const [sourcesOpen, setSourcesOpen] = useState<'subtitle' | 'video' | null>(null)
  const [aiSettingsOpen, setAiSettingsOpen] = useState(false)
  const [previewOpen, setPreviewOpen] = useState(false)
  /** 字幕内容预览（与字幕提取页共用弹窗）；null 表示关着 */
  const [subtitlePreview, setSubtitlePreview] = useState<SubtitlePreviewTarget | null>(null)

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

  /**
   * 已选字幕对应的历史产物条目，预览按钮要从它拿「任务 id + 序号」。
   *
   * 按路径回查 flow.sources —— 与上面的视频预览地址同一个路子。本地 .srt
   * 不在这份清单里，查不到就不显示预览按钮：后端那个预览接口的路径完全由
   * 任务记录推导（不让前端传路径），本地文件根本没有对应的入口。
   */
  const subtitleSource = useMemo(
    () => flow.sources?.subtitles.find((item) => item.path === flow.subtitle?.path) ?? null,
    [flow.sources, flow.subtitle],
  )

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

  /**
   * 口播语速的两个口径 + 字数预算。
   *
   * - `effectiveRate`：**下一次任务**将生效的语速 = 输入框覆盖值 ?? 全局默认，
   *   第 ① 步输入框与提示跟着它走；
   * - `jobRate`：**当前任务**的固化值（copyJob.chars_per_second，创建时快照；
   *   老任务是 0 = 不可考，回落输入框口径）。预算行、进度卡、每条文案的
   *   「约念几秒」都用它 —— 历史任务的字数 ÷ 当时的语速才对得上视频时长。
   *
   * 时长取**任务记录里的 video_duration**（后端 ffprobe 实测），不是素材卡片
   * 上那个 —— 本地文件的 `SelectedMaterial.duration_seconds` 恒为 null。
   * 预算算式与后端 char_budget 同源（见 copyLength.ts），页面这一份只用于显示。
   */
  const effectiveRate = flow.rateOverride ?? env?.chars_per_second ?? 0
  const jobRate = copyJob && copyJob.chars_per_second > 0 ? copyJob.chars_per_second : effectiveRate
  const budget = copyJob ? copyCharBudget(copyJob.video_duration, jobRate) : null

  /**
   * 第 ② 步右列正在看的那条候选（Tab 键 = candidate.key 的字符串）。
   *
   * 纯视图状态：候选本身在 flow 里，这里只管「在看哪一条」。键跟着候选列表
   * 回落 —— 换一批、从历史点开都会换掉整批候选，旧键落空时取第一条，免得
   * Tabs 找不到键渲染出一片空白（不用 effect 重置：那会先闪一帧空的）。
   */
  const [activeTabKey, setActiveTabKey] = useState('')
  const candidates = flow.candidates
  const activeCopyKey = candidates.some((item) => String(item.key) === activeTabKey)
    ? activeTabKey
    : candidates.length > 0
      ? String(candidates[0].key)
      : ''

  return (
    <Flex vertical gap={16}>
      {api.contextHolder}

      {envError && (
        <Alert type="error" showIcon message="环境探测失败" description={envError} />
      )}
      {/* 判据是 ai.ok 而不是 env.ready：ready 还要 ffmpeg-drawtext 与中文字体，
          那是烧字环境的条件，页面摘掉烧录后与剩下的功能无关（本机 brew 的 ffmpeg
          没有 drawtext，用 ready 会永远挂着一条无用警告） */}
      {env && !env.ai.ok && (
        <Alert
          type="warning"
          showIcon
          message="文案生成的环境还没就绪"
          description={<Text>· AI：{env.ai.detail}。{env.ai.fix_hint}</Text>}
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
      {env?.ai.ok && (
        <Alert
          type="success"
          showIcon
          message={`文案环境就绪：${env.ai.model} · 默认口播语速 ${env.chars_per_second} 字/秒`}
          action={
            <Button size="small" icon={<ApiOutlined />} onClick={() => setAiSettingsOpen(true)}>
              AI 配置与口播语速
            </Button>
          }
        />
      )}

      <Steps
        current={flow.step}
        items={[
          { title: '选择素材', description: '字幕 + 成片视频' },
          { title: 'AI 文案', description: '按口播语速定量，生成候选文案' },
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
                {/* 只看得到文件名和大小，选之前很难判断选对没有 —— 与成片卡片
                    的「预览」对称给一个。本地 .srt 没有对应接口，按钮不出现。 */}
                {subtitleSource && (
                  <Button
                    type="text"
                    size="small"
                    icon={<FileTextOutlined />}
                    onClick={() =>
                      setSubtitlePreview({
                        jobId: subtitleSource.job_id,
                        index: subtitleSource.index,
                      })
                    }
                  >
                    预览
                  </Button>
                )}
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

          <Flex vertical align="center" gap={8}>
            {/* 语速放在按钮正上方：改了它，下面这行换算立刻跟着变 */}
            <RateInput
              value={flow.rateOverride ?? env?.chars_per_second ?? null}
              onChange={flow.setRateOverride}
            />
            {/* 历史产物素材知道时长，直接折算这条视频的字数；本地视频的时长
                建任务时才由后端 probe，退回「每分钟约多少字」 */}
            {effectiveRate > 0 && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {flow.video?.duration_seconds
                  ? `这条视频约 ${Math.round(flow.video.duration_seconds * effectiveRate)} 字`
                  : `按 ${effectiveRate} 字/秒，每分钟约 ${Math.round(effectiveRate * 60)} 字`}
              </Text>
            )}
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
          <Flex justify="space-between" align="center" gap={8} wrap>
            <Button icon={<ArrowLeftOutlined />} onClick={flow.goBack}>
              返回重选素材
            </Button>
            {/* 输入框是「下一次生成」的值，下方预算行与进度卡是当前任务的
                固化值 —— 改这里预算行不动是正确语义，靠标注消解误会 */}
            <Space size={12} align="center" wrap>
              <RateInput
                value={flow.rateOverride ?? env?.chars_per_second ?? null}
                onChange={flow.setRateOverride}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                下次生成生效
              </Text>
              <Button
                icon={<ReloadOutlined />}
                disabled={flow.copyJobRunning}
                loading={flow.copyJobSubmitting}
                onClick={() => void flow.regenerate()}
              >
                换一批
              </Button>
            </Space>
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
                { label: '口播语速', value: jobRate > 0 ? `${jobRate} 字/秒` : '—' },
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

          {/* 预算提示放在 Tabs **上方**：任务跑完（JobProgressCard 消失）之后
              仍要看得见 —— 「这条该写多少字」是读候选文案的前提，每条 Tab 里的
              字数核对也以它为准。语速没探测到时只报时长。 */}
          {copyJob && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              这条视频 {formatDuration(copyJob.video_duration)}
              {jobRate > 0 && budget
                ? `，按 ${jobRate} 字/秒算，每条文案 ${budget[0]}–${budget[1]} 字`
                : ''}
              （念满视频时长，含标点、不含换行）
            </Text>
          )}

          {/* 左列字幕、右列 AI 产物，同屏对照 —— 这样才看得出 AI 是「没读懂
              素材」还是「读懂了但写得差」。左列的渲染条件与 copyResult 无关：
              失败、取消、正在跑的时候，恰恰最需要看 AI 读到的到底是什么。 */}
          <Row gutter={[16, 16]}>
            <Col xs={24} lg={10} xl={9}>
              {copyJob && (
                <SubtitleMaterialCard
                  data={flow.subtitleText}
                  loading={flow.subtitleTextLoading}
                  error={flow.subtitleTextError}
                />
              )}
            </Col>

            <Col xs={24} lg={14} xl={15}>
              {copyResult && (
                <Flex vertical gap={12}>
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

                  {/* 一条候选一个 Tab：一次生成五六条是常态，平铺成一列会把
                      页面拉得很长，切着看更省事 */}
                  <Tabs
                    activeKey={activeCopyKey}
                    onChange={setActiveTabKey}
                    items={candidates.map((state) => {
                      const candidate = copyResult.copies[state.key]
                      return {
                        key: String(state.key),
                        label: (
                          <CandidateTabLabel index={state.key} angle={candidate.angle} />
                        ),
                        children: (
                          <CopyCard
                            candidate={candidate}
                            state={state}
                            onTextChange={(text) => flow.updateCandidateText(state.key, text)}
                            charsPerSecond={jobRate}
                            budget={budget}
                          />
                        ),
                      }
                    })}
                  />
                </Flex>
              )}
            </Col>
          </Row>
        </>
      )}

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
                  <Space size={8} style={{ minWidth: 0 }}>
                    <Text>{item.name}</Text>
                    {/* 备注来自来源任务（同一条任务的产物共用一条）：清单里几条
                        「字幕 #12 / a.srt」长得一模一样，备注才是用户认得出的标记。
                        没写就整个不渲染，不给空 Tag 占位置。 */}
                    {item.remark?.trim() ? (
                      <Tag style={{ marginInlineEnd: 0 }}>{item.remark}</Tag>
                    ) : null}
                  </Space>
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

      {/* 本地文件选择（DirectoryPicker 的文件模式：传了 fileExtensions 才列出文件；
          这种用途只选文件，不必给 onSelect） */}
      <DirectoryPicker
        open={picker.active === 'subtitleFile'}
        title="选择字幕文件"
        fileExtensions={SUBTITLE_EXTENSIONS}
        onSelectFile={(path) => flow.selectSubtitle(localFileToMaterial(path))}
        onClose={picker.close}
      />
      <DirectoryPicker
        open={picker.active === 'videoFile'}
        title="选择成片视频"
        fileExtensions={VIDEO_EXTENSIONS}
        onSelectFile={(path) => flow.selectVideo(localFileToMaterial(path))}
        onClose={picker.close}
      />

      <SubtitlePreviewModal
        target={subtitlePreview}
        onClose={() => setSubtitlePreview(null)}
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
        showCharsPerSecond
      />

      {/* ---------- 备注编辑 ---------- */}
      {copyRemark.editing && (
        <JobRemarkModal
          job={copyRemark.editing}
          save={updateCopyJobRemark}
          onClose={copyRemark.close}
          onSaved={copyRemark.handleSaved}
        />
      )}
    </Flex>
  )
}
