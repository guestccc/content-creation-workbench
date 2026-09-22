/**
 * 一键换背景页面。
 *
 * 流程：选原图目录 → 勾选多张 → 选一张背景图 → 开始换背景 → 看进度 → 看产物。
 *
 * 与其它任务型页面同构：任务由后端异步执行，页面用轮询拿进度。三处不同：
 * 1. **没有环境自检** —— 抠图是纯 numpy 运算，没有外部工具要装；
 * 2. **取消不是即时的** —— 没有子进程可杀，当前这张会算完才收手，延迟上界 =
 *    单张图的处理时间。停止确认框里如实写了这一点；
 * 3. **产物就是进度明细** —— 每张原图对应一张 PNG，所以明细表里直接带缩略图，
 *    不再单独开一个产物清单（同一份 items 渲染两遍没有意义）。
 *
 * 页面自己不写状态机：原图目录扫描与勾选在本文件夹的 useImageDir 里（单页私有，
 * 不进公共 hooks），历史列表、任务生命周期、备注、清产物勾选分别在公共的
 * useJobList / useJobRunner / useJobRemark / usePurgeFiles 里，这里只做编排与布局。
 * 明细表（JobItemsTable）与历史「查看」弹窗（JobDetailModal）也在这个文件夹里 ——
 * 只有本页用得到，不进 src/components/。
 *
 * 除了自己挑目录，还能被**带着图跳进来**：素材抓取的结果弹窗里点「换背景」，
 * 把那条笔记已下载的图片（目录 + 文件名）放进路由 state，这里读出来预填。
 */

import { BgColorsOutlined, PictureOutlined } from '@ant-design/icons'
import type { ReactNode } from 'react'
import { useState } from 'react'
import { useLocation } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Flex,
  Image,
  InputNumber,
  Popconfirm,
  Row,
  Select,
  Space,
  Switch,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType, TableProps } from 'antd/es/table'

import DirectoryPicker from '../../components/DirectoryPicker'
import HistoryCard from '../../components/HistoryCard'
import JobProgressCard, { JobTitle } from '../../components/JobProgressCard'
import JobRemarkModal from '../../components/JobRemarkModal'
import PathBox from '../../components/PathBox'
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
} from '../../hooks'
import type { UsePurgeFilesResult } from '../../hooks'
import {
  batchDeleteBackgroundJobs,
  cancelBackgroundJob,
  createBackgroundJob,
  deleteBackgroundJob,
  fetchBackgroundJob,
  fetchBackgroundJobs,
  retryBackgroundItem,
  retryBackgroundJob,
  updateBackgroundJobRemark,
} from '../../api/background'
import { localImagePreviewUrl } from '../../api/filesystem'
import {
  DEFAULT_BACKGROUND_PARAMS,
  JOB_STATUS_META,
  POS_OPTIONS,
  isTerminalStatus,
} from '../../types/background'
import type {
  BackgroundJob,
  BackgroundJobItem,
  BackgroundJobParams,
  BackgroundJobPayload,
  BackgroundSwapPrefill,
} from '../../types/background'
import { formatElapsed } from '../../utils/format'
import ImageSourceCard from './ImageSourceCard'
import JobDetailModal from './JobDetailModal'
import JobItemsTable from './JobItemsTable'
import { useImageDir } from './useImageDir'

const { Text, Title, Paragraph } = Typography

/**
 * 背景图可选的后缀（与后端 BACKGROUND_INPUT_EXTENSIONS 一致）。
 *
 * 只用来过滤选择器里列出的文件 —— 真正的白名单校验在后端，这里宽一点窄一点
 * 都不构成安全问题，只是别让用户挑到一个后端拒收的文件。
 */
const BACKGROUND_EXTENSIONS = ['.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff']

/** 目录选择器弹窗的标题：同一个弹窗服务三种用途 */
const PICKER_TITLES = {
  input: '选择原图目录',
  output: '选择输出目录',
  bg: '选择背景图',
} as const

export default function BackgroundSwap() {
  const { message, fail, contextHolder } = useApiMessage()

  /**
   * 其它页面带过来的原图（react-router state）。
   *
   * 用 useState 的初始化器取一次：state 会一直挂在这条 history 记录上，
   * 但预填只在**进入页面这一刻**算数 —— 用户之后换了目录，这段说明就不该再挂着，
   * 所以手动换目录时把它一起清掉（见下面的 DirectoryPicker）。
   */
  const location = useLocation()
  const [prefill, setPrefill] = useState<BackgroundSwapPrefill | null>(
    () => (location.state ?? null) as BackgroundSwapPrefill | null,
  )

  const dir = useImageDir(fail, prefill)
  const picker = useDirectoryPicker<'input' | 'output' | 'bg'>()

  // ---------- 历史任务「查看」弹窗 ----------
  const [detailJobId, setDetailJobId] = useState<number | null>(null)
  const [detailJob, setDetailJob] = useState<BackgroundJob | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)

  const closeJobDetail = () => {
    setDetailJobId(null)
    setDetailJob(null)
  }

  // ---------- 输入与算法参数 ----------
  const [outputDir, setOutputDir] = useState('')
  /** 背景图绝对路径（空串表示还没选） */
  const [background, setBackground] = useState('')
  const [params, setParams] = useState<BackgroundJobParams>(DEFAULT_BACKGROUND_PARAMS)

  /** 改一个算法参数（高级设置里的每一项都走它） */
  const setParam = <K extends keyof BackgroundJobParams>(
    key: K,
    value: BackgroundJobParams[K],
  ) => {
    setParams((current) => ({ ...current, [key]: value }))
  }

  const history = useJobList<BackgroundJob>({ fetchList: fetchBackgroundJobs })

  // 历史表「备注」列的编辑开关：保存成功后就地刷新列表
  const remark = useJobRemark<BackgroundJob>({ message, onSaved: history.reload })

  // 删除时是否连产物一起清（每个删除确认框里都有这个勾选项）
  const purge = usePurgeFiles()

  const runner = useJobRunner<BackgroundJob, BackgroundJobPayload>({
    create: createBackgroundJob,
    cancel: cancelBackgroundJob,
    // take() 在这里调用：删除那一刻取值并重置，勾选只对这一次删除有效
    remove: (jobId) => deleteBackgroundJob(jobId, purge.take()),
    batchRemove: (ids) => batchDeleteBackgroundJobs(ids, purge.take()),
    fetchJob: fetchBackgroundJob,
    isTerminal: (job) => isTerminalStatus(job.status),
    fail,
    onChanged: history.reload,
    onRemoved: (jobId) => {
      // 正在弹窗里看这条：记录没了就别让它挂在那儿
      if (detailJobId === jobId) {
        closeJobDetail()
      }
    },
  })

  // 弹窗里那条任务还在跑时同样轮询（历史列表里点开正在跑的任务是常事）：
  // 明细表里的状态才不是打开那一刻的快照。轮询由 useJobPolling 自己按
  // 「哪条任务、是否在跑」判断，终态一到就停。
  useJobPolling({
    job: detailJob,
    fetchJob: fetchBackgroundJob,
    isTerminal: (job) => isTerminalStatus(job.status),
    onUpdate: setDetailJob,
  })

  const job = runner.job

  const canSubmit =
    Boolean(dir.path) && Boolean(background) && !runner.running && !runner.submitting

  /** 创建任务 */
  const start = async () => {
    if (!dir.path) {
      message.warning('请先选择原图目录')
      return
    }
    if (dir.images.length === 0 && dir.selected.length === 0) {
      message.warning('该目录下没有可处理的图片文件')
      return
    }
    if (!background) {
      message.warning('请先选择一张背景图')
      return
    }

    const created = await runner.submit({
      input_path: dir.path,
      background_path: background,
      params,
      ...(outputDir ? { output_dir: outputDir } : {}),
      ...(dir.selected.length > 0 ? { files: dir.selected } : {}),
    })
    if (!created) {
      return
    }
    message.success('已开始换背景，可以在下面看进度')
  }

  /** 取消任务 */
  const cancel = async (jobId: number) => {
    if (await runner.cancel(jobId)) {
      message.info('已请求取消，当前这张算完就停')
    }
  }

  /**
   * 重试接口返回的是**整条任务**的新状态，三处状态源都要对上号地刷新：
   * 页面上方那张当前任务卡、历史「查看」弹窗里那条、历史列表的行。
   * 对不上号的保持原样，互不干扰（与 SceneSplit 的收尾一致）。
   */
  const applyRetried = (updated: BackgroundJob) => {
    runner.setJob((current) => (current?.id === updated.id ? updated : current))
    setDetailJob((current) => (current?.id === updated.id ? updated : current))
    history.reload()
  }

  /** 重试单张失败 / 跳过的图片：任务重新入队，其余图片的结果不动 */
  const retryItem = async (target: BackgroundJob, item: BackgroundJobItem) => {
    try {
      applyRetried(await retryBackgroundItem(target.id, item.index))
      message.success(`已重新入队：${item.source_name}`)
    } catch (error) {
      fail(error, '重试失败')
    }
  }

  /**
   * 重试全部未完成的图片。
   *
   * 走一条批量接口而不是循环调单条：第一次调用就把任务置回 pending，循环里
   * 第二次会撞上「任务尚未结束」的 409 而半途而废。
   */
  const retryAll = async (target: BackgroundJob) => {
    const count = target.failed_images + target.skipped_images
    try {
      applyRetried(await retryBackgroundJob(target.id))
      message.success(`已重新入队 ${count} 张`)
    } catch (error) {
      fail(error, '重试失败')
    }
  }

  /** 删除任务记录（是否连产物图一起删由确认框里的勾选决定） */
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
  const historyRowSelection: TableProps<BackgroundJob>['rowSelection'] = {
    selectedRowKeys: history.selectedRowKeys,
    onChange: (keys) => history.setSelectedRowKeys(keys.map(Number)),
    getCheckboxProps: (job) => ({ disabled: !isTerminalStatus(job.status) }),
  }

  /**
   * 历史列表点「查看」：读回这条任务，在弹窗里看它的参数与逐张明细。
   *
   * 不挂到页面上方那张「当前任务」卡片里：那会让正在跑的任务从卡片上消失
   * （进度与轮询都跟着换人）。历史里点开的本来就是另一条任务，弹窗看最干净。
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

  return (
    <div className="page">
      {contextHolder}

      <div className="page__header">
        <Title level={3} style={{ marginBottom: 4 }}>
          <PictureOutlined style={{ marginRight: 8 }} />
          一键换背景
        </Title>
        <Paragraph type="secondary" style={{ marginBottom: 0 }}>
          给一批手绘 / 涂鸦图抠出线条，统一贴到同一张背景上，产出可直接发布的整图。
        </Paragraph>
      </div>

      {/* 适用范围：这个算法只做「浅底 + 深色线条」，不说清会让普通照片白跑一趟 */}
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="适用于浅底深色线条的手绘 / 涂鸦"
        description="拍在木桌、纸面上的手绘线条能抠得比较干净；人像、商品这类普通照片不在本功能的适用范围，换背景请用别的工具。"
      />

      {/* ---------- 输入：原图目录 + 勾选 / 背景图 + 参数 ---------- */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col xs={24} lg={14}>
          <ImageSourceCard
            dir={dir}
            outputDir={outputDir}
            prefillNotice={
              prefill
                ? `已从${prefill.source}带入这 ${prefill.files.length} 张图并勾选，可自行增减`
                : undefined
            }
            onPickInput={() => picker.open('input')}
            onPickOutput={() => picker.open('output')}
          />
        </Col>

        <Col xs={24} lg={10}>
          <Card
            title={
              <Space size={8}>
                <BgColorsOutlined style={{ color: 'var(--color-primary)' }} />
                背景图与贴合参数
              </Space>
            }
            style={{ height: '100%' }}
          >
            <Space direction="vertical" size={16} style={{ width: '100%' }}>
              <div>
                <Text type="secondary">背景图（一次任务只用一张）</Text>
                <Space.Compact style={{ width: '100%', marginTop: 4 }}>
                  <Button onClick={() => picker.open('bg')}>选择背景图</Button>
                  <PathBox value={background} placeholder="尚未选择" />
                </Space.Compact>
                {background && (
                  <Flex align="center" gap={12} style={{ marginTop: 8 }}>
                    <Image
                      src={localImagePreviewUrl(background)}
                      width={72}
                      height={72}
                      style={{ objectFit: 'cover', borderRadius: 6 }}
                    />
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      点击图片可放大确认
                    </Text>
                  </Flex>
                )}
              </div>

              {/* 高级设置：默认值就是推荐值，收起来不打扰 */}
              <Collapse
                ghost
                items={[
                  {
                    key: 'advanced',
                    label: (
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        高级设置（默认值即推荐值）
                      </Text>
                    ),
                    children: <AdvancedSettings params={params} setParam={setParam} />,
                  },
                ]}
              />
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
              icon={<BgColorsOutlined />}
              onClick={() => void start()}
              loading={runner.submitting}
              disabled={!canSubmit}
            >
              开始换背景
            </Button>
            {runner.running && job && (
              <Popconfirm
                title="取消当前任务？"
                description="抠图没有子进程可停：当前这张会算完才收手（大图可能要十几秒）。已产出的图保留，剩余图片标记为跳过。"
                okText="取消任务"
                cancelText="继续跑"
                onConfirm={() => void cancel(job.id)}
              >
                <Button danger>停止</Button>
              </Popconfirm>
            )}
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            同时只跑一个任务，后来的排队等待；产物按任务分目录存放
          </Text>
        </Flex>
      </Card>

      {/* ---------- 进度与产物明细 ---------- */}
      {job && (
        <JobProgressCard
          title={<JobTitle jobId={job.id} meta={JOB_STATUS_META[job.status]} />}
          status={job.status}
          percent={job.progress_percent}
          running={runner.running}
          stats={[
            { label: '图片进度', value: `${job.completed_images} / ${job.total_images} 张` },
            {
              label: '失败',
              value:
                job.failed_images > 0 ? (
                  <Text type="danger">{job.failed_images} 张</Text>
                ) : (
                  '0 张'
                ),
            },
            { label: '跳过', value: `${job.skipped_images} 张` },
            {
              label: '耗时',
              value: job.started_at
                ? formatElapsed((Date.now() - new Date(job.started_at).getTime()) / 1000)
                : '—',
            },
          ]}
          current={
            job.current_image
              ? {
                  label: '处理',
                  index: job.current_index,
                  total: job.total_images,
                  name: job.current_image,
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
          {/* 每张原图的处理明细与产物：这张表就是产物清单 */}
          <div style={{ marginTop: 12 }}>
            <JobItemsTable
              job={job}
              onRetryItem={(item) => void retryItem(job, item)}
              onRetryAll={(target) => void retryAll(target)}
            />
          </div>
        </JobProgressCard>
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

      {/* ---------- 目录选择器（三种用途共用一个弹窗） ---------- */}
      <DirectoryPicker
        open={picker.active !== null}
        title={PICKER_TITLES[picker.active ?? 'input']}
        initialPath={
          picker.active === 'output' ? outputDir || dir.path : dir.path || undefined
        }
        // 背景图还要列文件：不传 fileExtensions 时列表里只有目录
        fileExtensions={picker.active === 'bg' ? BACKGROUND_EXTENSIONS : undefined}
        onSelect={(path) => {
          // 三种用途共用一个弹窗，这里**逐个认**、不写 else 兜底：
          // 早先写成 else 时，'bg'（文件模式，选定文件走 onSelectFile）也会落到
          // 分支里，用户选背景图时点了「选定此目录」就把原图目录改掉了。
          if (picker.active === 'input') {
            dir.setPath(path)
            // 用户自己重新选了原图目录：带入的那批图与说明都不再适用
            setPrefill(null)
          } else if (picker.active === 'output') {
            setOutputDir(path)
          }
        }}
        onSelectFile={(path) => setBackground(path)}
        onClose={picker.close}
      />

      {/* ---------- 历史任务详情弹窗 ---------- */}
      <JobDetailModal
        jobId={detailJobId}
        job={detailJob}
        loading={detailLoading}
        onClose={closeJobDetail}
        onRetryItem={(item) => {
          if (detailJob) {
            void retryItem(detailJob, item)
          }
        }}
        onRetryAll={(target) => void retryAll(target)}
      />

      {/* ---------- 备注编辑 ---------- */}
      {remark.editing && (
        <JobRemarkModal
          job={remark.editing}
          save={updateBackgroundJobRemark}
          onClose={remark.close}
          onSaved={remark.handleSaved}
        />
      )}
    </div>
  )
}

/**
 * 高级设置：把脚本的全部参数摆出来，默认值就是脚本默认。
 *
 * 分两组只是便于找 —— 上面那组决定「贴到哪儿、多大」，下面那组决定「怎么抠」。
 */
function AdvancedSettings({
  params,
  setParam,
}: {
  params: BackgroundJobParams
  setParam: <K extends keyof BackgroundJobParams>(
    key: K,
    value: BackgroundJobParams[K],
  ) => void
}) {
  return (
    <Flex vertical gap={18}>
      <Text strong style={{ fontSize: 12 }}>
        贴合
      </Text>

      <Field label="贴合位置" hint="「自动找落点」会在背景的空白处落点，不走中心">
        <Select
          value={params.pos ?? ''}
          onChange={(value) => setParam('pos', value || null)}
          options={POS_OPTIONS}
          style={{ width: 150 }}
        />
      </Field>

      <NumberField
        label="缩放比例"
        hint="贴纸宽度占背景宽度的比例，0.1 = 占背景宽的 10%；留空 = 不缩放，按原尺寸贴"
        value={params.scale}
        placeholder="原尺寸"
        min={0.01}
        max={10}
        step={0.05}
        onChange={(value) => setParam('scale', value)}
      />

      <NumberField
        label="旋转角度"
        hint="正值顺时针；贴歪了的手绘可以靠它摆正"
        value={params.rotate}
        min={-360}
        max={360}
        step={1}
        addon="°"
        onChange={(value) => setParam('rotate', value ?? DEFAULT_BACKGROUND_PARAMS.rotate)}
      />

      <NumberField
        label="不透明度"
        hint="贴纸整体的不透明度，1 = 完全不透明"
        value={params.opacity}
        min={0}
        max={1}
        step={0.05}
        onChange={(value) => setParam('opacity', value ?? DEFAULT_BACKGROUND_PARAMS.opacity)}
      />

      <NumberField
        label="自动落点起点"
        hint="距背景顶部的比例，只在「自动找落点」时生效：太小会贴到页眉上"
        value={params.search_from}
        min={0}
        max={1}
        step={0.05}
        onChange={(value) =>
          setParam('search_from', value ?? DEFAULT_BACKGROUND_PARAMS.search_from)
        }
      />

      <NumberField
        label="离边距离"
        hint="贴纸离背景边缘至少留这么多像素"
        value={params.margin}
        min={0}
        max={10000}
        step={1}
        addon="px"
        onChange={(value) => setParam('margin', value ?? DEFAULT_BACKGROUND_PARAMS.margin)}
      />

      <Text strong style={{ fontSize: 12 }}>
        抠图
      </Text>

      <NumberField
        label="纸的透明线"
        hint="亮度高于这个比例的像素判为纯纸（完全透明）"
        value={params.hi_frac}
        min={0}
        max={1}
        step={0.01}
        onChange={(value) => setParam('hi_frac', value ?? DEFAULT_BACKGROUND_PARAMS.hi_frac)}
      />

      <NumberField
        label="墨的实心线"
        hint="亮度低于这个比例的像素判为纯墨（完全不透明）；必须小于透明线"
        value={params.lo_frac}
        min={0}
        max={1}
        step={0.01}
        onChange={(value) => setParam('lo_frac', value ?? DEFAULT_BACKGROUND_PARAMS.lo_frac)}
      />

      <NumberField
        label="主体定位线"
        hint="留空 = 自动扫档位；手填 0–1 可强制指定，背景被圈进来时调它"
        value={params.dark_frac}
        placeholder="自动"
        min={0}
        max={1}
        step={0.01}
        onChange={(value) => setParam('dark_frac', value)}
      />

      <NumberField
        label="纸纹截断"
        hint="低于这个透明度的淡灰直接归零，否则整张会蒙一层雾"
        value={params.pedestal}
        min={0}
        max={1}
        step={0.01}
        onChange={(value) => setParam('pedestal', value ?? DEFAULT_BACKGROUND_PARAMS.pedestal)}
      />

      <Field label="位置门" hint="主体包围盒之外整片置零；背景不是纯色时必须开">
        <Switch checked={params.gate} onChange={(checked) => setParam('gate', checked)} />
      </Field>

      <NumberField
        label="位置门扩边"
        hint="包围盒往外扩的比例，给抗锯齿的边缘留余量"
        value={params.gate_pad}
        min={0}
        max={1}
        step={0.01}
        onChange={(value) => setParam('gate_pad', value ?? DEFAULT_BACKGROUND_PARAMS.gate_pad)}
      />

      <Field label="暖色抑制" hint="按彩度压掉主体旁边的木纹、暖色桌面">
        <Switch checked={params.warm} onChange={(checked) => setParam('warm', checked)} />
      </Field>

      <Field
        label="保留彩色"
        hint="默认把线条统一成墨色，避免深色背景上出现一圈白边；要贴彩色主体时才打开"
      >
        <Switch
          checked={params.keep_color}
          onChange={(checked) => setParam('keep_color', checked)}
        />
      </Field>
    </Flex>
  )
}

/** 高级设置里的通用一行：左边说明、右边控件 */
function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint: string
  children: ReactNode
}) {
  return (
    <Flex justify="space-between" align="flex-start" gap={16}>
      <div style={{ minWidth: 0 }}>
        <Text>{label}</Text>
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {hint}
          </Text>
        </div>
      </div>
      {children}
    </Flex>
  )
}

/** 高级设置里的数值一行（清空时由调用方决定退回默认值还是「自动」） */
function NumberField({
  label,
  hint,
  value,
  placeholder,
  min,
  max,
  step,
  addon,
  onChange,
}: {
  label: string
  hint: string
  value: number | null
  placeholder?: string
  min: number
  max: number
  step: number
  addon?: string
  onChange: (value: number | null) => void
}) {
  return (
    <Field label={label} hint={hint}>
      <InputNumber
        value={value}
        placeholder={placeholder}
        min={min}
        max={max}
        step={step}
        addonAfter={addon}
        style={{ width: 150 }}
        onChange={onChange}
      />
    </Field>
  )
}

/** 历史任务表格列定义 */
function historyColumns(handlers: {
  onView: (jobId: number) => void
  onCancel: (jobId: number) => void
  onDelete: (jobId: number) => void
  onEditRemark: (job: BackgroundJob) => void
  onRetry: (job: BackgroundJob) => void
  purge: UsePurgeFilesResult
}): ColumnsType<BackgroundJob> {
  return [
    jobIdColumn<BackgroundJob>(),
    jobStatusColumn<BackgroundJob>(JOB_STATUS_META),
    jobInputColumn<BackgroundJob>(),
    { title: '图片', dataIndex: 'total_images', width: 64 },
    {
      title: '背景',
      dataIndex: 'background_name',
      width: 140,
      ellipsis: true,
      render: (value: string, job: BackgroundJob) => (
        <Tooltip title={job.background_path}>
          <Text style={{ fontSize: 12 }}>{value}</Text>
        </Tooltip>
      ),
    },
    { title: '失败', dataIndex: 'failed_images', width: 64 },
    jobRemarkColumn<BackgroundJob>({ onEdit: handlers.onEditRemark }),
    jobCreatedColumn<BackgroundJob>(),
    jobActionsColumn<BackgroundJob>({
      isTerminal: (job) => isTerminalStatus(job.status),
      onView: handlers.onView,
      onCancel: handlers.onCancel,
      onDelete: handlers.onDelete,
      deleteDescription: '删除后不可恢复。',
      purge: handlers.purge,
      // 列表接口不带逐张明细，只能靠行上的计数判断还有没有没产出的
      onRetry: handlers.onRetry,
      canRetry: (job) => job.failed_images + job.skipped_images > 0,
    }),
  ]
}
