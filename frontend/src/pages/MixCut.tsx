/**
 * 智能混剪页。
 *
 * 素材由用户自己添加目录（任意本地目录都行，不限于 materials/ 里），
 * 从这些目录里挑三批片段，按固定规则拼成成片：
 * - 开头列表：按用户选择的顺序拼接；
 * - 中间列表：用户手选一批，系统为每条成片各自随机打乱；
 * - 结尾列表：按用户选择的顺序拼接。
 *
 * 页面分区（与 SceneSplit 同风格）：
 * 1. 三段编排：三张卡片等宽同行（开头｜中间｜结尾，一眼看出它们是一条成片的三段）。
 *    开头/结尾是有序列表（↑↓ 调整、✕ 移除），中间是待打乱的一堆；
 *    每张卡片自己的「+ 添加素材」打开选片弹窗，弹窗里切目录、勾片段，确认后整批进这张卡片。
 *    素材目录的添加与移除也只在弹窗里做 —— 页面上没有单独的素材区。
 * 2. 合成设置：输出条数（「混剪 1 个 / 混剪多个」）、输出目录、画幅与时长预估；
 * 3. 进度：antd Progress，分阶段显示「归一化第 k/N 个片段」「拼接第 k/N 条成片」；
 * 4. 成品：每条一张卡片，封面 + 播放（Modal + 原生 <video>，Range 可拖进度条）；
 * 5. 历史任务：Table。
 *
 * 为什么不做独立素材区：素材可能来自任意目录，先在一个大网格里翻找、
 * 再回头确认「我到底往哪一段加了什么」，视线要来回跑。改成三段各自选，
 * 每张卡片的「+ 添加素材」直接决定这批素材的归属，选完立刻在卡片里看到结果。
 *
 * 页面自己不写状态机：素材库、任务生命周期、历史列表分别在 useAsyncData /
 * useJobRunner / useJobList 里，这里只做编排与布局。
 */

import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  CheckOutlined,
  CloseOutlined,
  PlayCircleOutlined,
} from '@ant-design/icons'
import { useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Empty,
  Flex,
  Image,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Select,
  Space,
  Spin,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'

import {
  addMixSource,
  cancelMixJob,
  createMixJob,
  deleteMixJob,
  fetchMixEnvironment,
  fetchMixJob,
  fetchMixJobs,
  fetchMixLibrary,
  removeMixSource,
} from '../api/mix'
import DirectoryPicker from '../components/DirectoryPicker'
import HistoryCard from '../components/HistoryCard'
import VideoPreviewModal from '../components/VideoPreviewModal'
import { jobStatusColumn } from '../components/jobColumns'
import {
  useApiMessage,
  useAsyncData,
  useDirectoryPicker,
  useJobList,
  useJobRunner,
} from '../hooks'
import {
  JOB_STATUS_META,
  OUTPUT_STATUS_META,
  isTerminalStatus,
  middlePermutationLimit,
} from '../types/mix'
import type {
  MixClip,
  MixEnvironment,
  MixJob,
  MixJobPayload,
  MixOutputItem,
} from '../types/mix'
import { formatBytes, formatDuration } from '../utils/format'

const { Text, Title } = Typography

/** 一个素材目录最多渲染多少张卡片：几千条素材全铺出来会把页面拖垮 */
const MAX_RENDERED_CLIPS = 200

/** 三段列表的标识 */
type SectionKey = 'opening' | 'middle' | 'ending'

const SECTION_META: Record<
  SectionKey,
  { title: string; hint: string; color: string; hex: string }
> = {
  opening: { title: '开头', hint: '按你选择的顺序拼接', color: 'green', hex: '#52c41a' },
  middle: { title: '中间', hint: '合成时随机打乱', color: 'orange', hex: '#fa8c16' },
  ending: { title: '结尾', hint: '按你选择的顺序拼接', color: 'blue', hex: '#1677ff' },
}

/** 一条成片的三段顺序：开头 → 中间（每条不同）→ 结尾。与任务里存的三段长度一一对应 */
const ORDER_KEYS: SectionKey[] = ['opening', 'middle', 'ending']

/** 缩略图加载失败时的占位图（与镜头分割页同一张） */
const THUMB_FALLBACK =
  'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNjAiIGhlaWdodD0iOTYiPjxyZWN0IHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiIGZpbGw9IiMxYTFhMWEiLz48dGV4dCB4PSI1MCUiIHk9IjUwJSIgZmlsbD0iIzg4OCIgZm9udC1zaXplPSIxMiIgdGV4dC1hbmNob3I9Im1pZGRsZSIgZHk9Ii4zZW0iPuaXoOe8qeeVpTwvdGV4dD48L3N2Zz4='

/** 选片弹窗里已勾选卡片的角标：有序列表显示「第几个被选中」，中间列表显示对钩 */
const PICKED_BADGE: React.CSSProperties = {
  position: 'absolute',
  left: 4,
  top: 4,
  minWidth: 18,
  height: 18,
  padding: '0 4px',
  background: '#1677ff',
  color: '#fff',
  borderRadius: 9,
  fontSize: 11,
  // 徽标里既可能是一个序号、也可能是一个对勾图标，用 flex 居中，
  // 不靠 line-height 对齐（图标的基线跟文字不一样）
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
}

/** 已选卡片的描边色（与 Tag/Button 的主色一致） */
const PICKED_BORDER = '#1677ff'

/**
 * 列表里的小缩略图。
 *
 * clip 可能拿不到（素材目录被移除后，列表里会留下解析不到的 id），
 * 这种情况显示「已失效」而不是崩掉 —— 用户能看见问题出在哪一条。
 */
function ClipThumb({
  clip,
  width,
  height = 40,
  onClick,
}: {
  clip?: MixClip
  width: number
  height?: number
  onClick?: () => void
}) {
  const box: React.CSSProperties = {
    width,
    height,
    flex: 'none',
    background: '#000',
    borderRadius: 4,
    overflow: 'hidden',
    position: 'relative',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    cursor: onClick ? 'pointer' : 'default',
  }
  if (!clip) {
    return (
      <div style={box}>
        <Text type="secondary" style={{ fontSize: 10 }}>
          已失效
        </Text>
      </div>
    )
  }
  return (
    <div style={box} onClick={onClick} title={onClick ? '点击预览' : undefined}>
      <Image
        src={clip.thumb_url}
        alt={clip.name}
        preview={false}
        width={width}
        height={height}
        style={{ objectFit: 'cover' }}
        fallback={THUMB_FALLBACK}
      />
    </div>
  )
}

/**
 * 把一条成片的 order 切成 开头/中间/结尾 三段。
 *
 * 任务落库时 order 就是 opening + middle + ending 首尾相接（见后端 plan_outputs），
 * 用任务上记录的三段长度就能切回来，不用额外字段。
 * 万一长度对不上（历史数据），多出来的尾巴归到最后一段，不悄悄丢掉。
 */
function splitOrder(
  job: MixJob,
  order: string[],
): { key: SectionKey; items: { path: string; index: number }[] }[] {
  const lengths: Record<SectionKey, number> = {
    opening: job.opening.length,
    middle: job.middle.length,
    ending: job.ending.length,
  }
  let cursor = 0
  return ORDER_KEYS.map((key, position) => {
    const end =
      position === ORDER_KEYS.length - 1 ? order.length : Math.min(cursor + lengths[key], order.length)
    const items = order
      .slice(cursor, end)
      .map((path, offset) => ({ path, index: cursor + offset }))
    cursor = end
    return { key, items }
  })
}

/**
 * 成片顺序条上的一小格。
 *
 * clip 可能拿不到（素材目录后来被移除、或素材被挪走），这时只给个占位方块 + 序号，
 * 鼠标悬停仍能看到文件名与原始路径 —— 顺序看得见，缺哪条也看得见。
 */
function OrderThumb({
  clip,
  rawPath,
  index,
  color,
  onClick,
}: {
  clip?: MixClip
  rawPath: string
  index: number
  color: string
  onClick: () => void
}) {
  const fileName = rawPath.split('/').pop() ?? rawPath
  return (
    <Tooltip title={`${index}. ${clip?.name ?? fileName}`}>
      <div
        style={{
          position: 'relative',
          width: 28,
          height: 28,
          flex: 'none',
          background: '#000',
          borderRadius: 3,
          overflow: 'hidden',
          border: `1px solid ${clip ? color : '#d9d9d9'}`,
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          boxSizing: 'border-box',
        }}
        onClick={onClick}
      >
        {clip ? (
          <Image
            src={clip.thumb_url}
            alt={clip.name}
            preview={false}
            width={26}
            height={26}
            style={{ objectFit: 'cover' }}
            fallback={THUMB_FALLBACK}
          />
        ) : (
          <Text style={{ fontSize: 9, color: '#888' }}>缺</Text>
        )}
        <span
          style={{
            position: 'absolute',
            left: 0,
            bottom: 0,
            minWidth: 12,
            height: 12,
            padding: '0 2px',
            boxSizing: 'border-box',
            background: 'rgba(0,0,0,0.65)',
            color: '#fff',
            fontSize: 9,
            lineHeight: '12px',
            textAlign: 'center',
          }}
        >
          {index}
        </span>
      </div>
    </Tooltip>
  )
}

/** 网格卡片的播放角标 */
const PLAY_BADGE: React.CSSProperties = {
  position: 'absolute',
  right: 6,
  bottom: 6,
  background: 'rgba(0,0,0,0.55)',
  color: '#fff',
  borderRadius: 4,
  padding: '0 6px',
  fontSize: 12,
  height: 18,
  // 里面是图标不是文字，用 flex 居中，别再靠 line-height
  display: 'inline-flex',
  alignItems: 'center',
}

export default function MixCut() {
  const { message, fail, contextHolder } = useApiMessage()
  const picker = useDirectoryPicker<'output' | 'source'>()

  // ---------- 三段编排 ----------
  const [opening, setOpening] = useState<string[]>([])
  const [middle, setMiddle] = useState<string[]>([])
  const [ending, setEnding] = useState<string[]>([])

  const sectionState: Record<SectionKey, [string[], (v: string[]) => void]> = {
    opening: [opening, setOpening],
    middle: [middle, setMiddle],
    ending: [ending, setEnding],
  }

  // ---------- 选片弹窗（一次只服务一个列表） ----------
  /** 正在给哪个列表选素材；null 表示弹窗关着 */
  const [pickerSection, setPickerSection] = useState<SectionKey | null>(null)
  /** 弹窗里当前浏览的素材目录 */
  const [pickSourceId, setPickSourceId] = useState('')
  /** 弹窗里的勾选结果。有序 —— 开头/结尾的拼接顺序就是它 */
  const [pickSelected, setPickSelected] = useState<string[]>([])
  /** 弹窗里的筛选关键字（按文件名/相对路径模糊匹配） */
  const [pickKeyword, setPickKeyword] = useState('')
  const [addingSource, setAddingSource] = useState(false)

  // ---------- 合成设置 ----------
  const [count, setCount] = useState(1)
  const [outputDir, setOutputDir] = useState('')

  // ---------- 预览弹窗 ----------
  const [previewClip, setPreviewClip] = useState<MixClip | null>(null)
  const [previewOutput, setPreviewOutput] = useState<MixOutputItem | null>(null)
  /** 顺序弹窗：正在看哪条成片的完整拼接顺序 */
  const [orderDetail, setOrderDetail] = useState<{ job: MixJob; output: MixOutputItem } | null>(
    null,
  )
  /** 历史任务的详情弹窗 */
  const [historyDetail, setHistoryDetail] = useState<MixJob | null>(null)

  // ---------- 后端数据 ----------
  const environment = useAsyncData({
    load: () => fetchMixEnvironment(),
    failMessage: '环境自检失败',
    // 环境自检失败不阻断页面：素材库与历史仍可看，告警区由 env 为 null 兜住
    silent: true,
    // 环境自检回来后，输出目录默认停在 materials/output（用户已经选过就不动）
    onLoaded: (data) => setOutputDir((current) => current || data.default_output_dir),
  })
  const env: MixEnvironment | null = environment.data

  const library = useAsyncData({
    load: () => fetchMixLibrary(),
    failMessage: '素材加载失败',
    fail,
    onLoaded: (data) => {
      // 弹窗里正看着的目录被移除了就切到第一个（空库时清空）
      setPickSourceId((prev) =>
        data.sources.some((source) => source.id === prev) ? prev : (data.sources[0]?.id ?? ''),
      )
    },
  })

  const history = useJobList<MixJob>({ fetchList: fetchMixJobs })

  const runner = useJobRunner<MixJob, MixJobPayload>({
    create: createMixJob,
    cancel: cancelMixJob,
    remove: deleteMixJob,
    fetchJob: fetchMixJob,
    isTerminal: (job) => isTerminalStatus(job.status),
    fail,
    onChanged: history.reload,
    onRemoved: () => setHistoryDetail(null),
  })

  const clips = library.data?.clips ?? []
  const sources = library.data?.sources ?? []

  const clipMap = useMemo(() => {
    const map = new Map<string, MixClip>()
    for (const clip of clips) {
      map.set(clip.id, clip)
    }
    return map
  }, [clips])

  /**
   * 按任务里存的路径反查素材。
   *
   * 任务的 order 存的是素材绝对路径，但改版前建的老任务存的是相对素材根的路径
   * （`clips/组/x.mp4`），所以三级命中：绝对路径 → 用素材根拼一次 → 退回按文件名找。
   * 都找不到就返回 undefined，界面上显示成「缺」而不是装作有。
   */
  const findClipByPath = useMemo(() => {
    const byAbs = new Map<string, MixClip>()
    const byName = new Map<string, MixClip>()
    for (const clip of clips) {
      byAbs.set(clip.abs_path, clip)
      if (!byName.has(clip.name)) {
        byName.set(clip.name, clip)
      }
    }
    const root = env?.materials_dir.replace(/\/+$/, '')
    return (raw: string): MixClip | undefined => {
      const direct = byAbs.get(raw)
      if (direct) {
        return direct
      }
      if (root && !raw.startsWith('/')) {
        const joined = byAbs.get(`${root}/${raw}`)
        if (joined) {
          return joined
        }
      }
      return byName.get(raw.split('/').pop() ?? raw)
    }
  }, [clips, env])

  // ---------- 选片弹窗操作 ----------
  /** 打开某一段的选片弹窗：把这列表已选的片段带进去，方便继续加或者去掉 */
  const openPicker = (section: SectionKey) => {
    setPickerSection(section)
    setPickSelected(sectionState[section][0])
    setPickKeyword('')
    setPickSourceId((prev) =>
      sources.some((source) => source.id === prev) ? prev : (sources[0]?.id ?? ''),
    )
  }

  /** 勾选/取消勾选。勾上的先后顺序，就是开头/结尾最终拼接的顺序 */
  const togglePick = (clipId: string) => {
    setPickSelected((prev) =>
      prev.includes(clipId) ? prev.filter((id) => id !== clipId) : [...prev, clipId],
    )
  }

  /** 确认：把这一段的列表整个换成弹窗里的勾选结果（顺序即勾选顺序） */
  const confirmPick = () => {
    if (pickerSection === null) {
      return
    }
    sectionState[pickerSection][1](pickSelected)
    message.success(`「${SECTION_META[pickerSection].title}」已加入 ${pickSelected.length} 条素材`)
    setPickerSection(null)
  }

  /** 添加素材目录：只登记目录，文件不会被复制或移动。加完直接切到这个新目录 */
  const handleAddSource = async (path: string) => {
    picker.close()
    setAddingSource(true)
    try {
      const source = await addMixSource(path)
      message.success(`已添加素材目录：${source.path}`)
      setPickSourceId(source.id)
      setPickKeyword('')
      await library.reload()
    } catch (error) {
      fail(error, '添加素材目录失败')
    } finally {
      setAddingSource(false)
    }
  }

  /** 移除素材目录：只是不再从它取素材，磁盘上的文件一个都不动 */
  const handleRemoveSource = async (sourceId: string) => {
    // 先按当前扫描结果算出这个目录下的片段，把它们从三个列表里一并摘掉 ——
    // 目录一移走，这些 id 就再也解析不到，留在列表里建任务时必然报「素材不存在」
    const doomed = new Set(clips.filter((clip) => clip.source_id === sourceId).map((clip) => clip.id))
    if (doomed.size > 0) {
      for (const [list, setter] of Object.values(sectionState)) {
        const kept = list.filter((id) => !doomed.has(id))
        if (kept.length !== list.length) {
          setter(kept)
        }
      }
      setPickSelected((prev) => prev.filter((id) => !doomed.has(id)))
    }
    try {
      await removeMixSource(sourceId)
      message.success(
        doomed.size > 0
          ? `已移除素材目录，其中 ${doomed.size} 条素材也从列表里摘掉了（磁盘文件保留）`
          : '已移除素材目录（磁盘文件保留）',
      )
      await library.reload()
    } catch (error) {
      fail(error, '移除素材目录失败')
    }
  }

  // ---------- 三段编排操作 ----------
  const removeFrom = (section: SectionKey, index: number) => {
    const [list, setter] = sectionState[section]
    setter(list.filter((_, i) => i !== index))
  }

  const moveIn = (section: SectionKey, index: number, delta: number) => {
    const [list, setter] = sectionState[section]
    const target = index + delta
    if (target < 0 || target >= list.length) {
      return
    }
    const next = [...list]
    ;[next[index], next[target]] = [next[target], next[index]]
    setter(next)
  }

  // ---------- 预估与校验（按钮上直接显示缺什么，不做「点了才报错」） ----------
  const estimate = useMemo(() => {
    const ids = [...opening, ...middle, ...ending]
    let total = 0
    for (const id of ids) {
      total += clipMap.get(id)?.duration ?? 0
    }
    return total
  }, [opening, middle, ending, clipMap])

  const permutationLimit = middlePermutationLimit(middle.length)
  const validationError = useMemo(() => {
    if (opening.length === 0) return '还没选开头片段'
    if (middle.length === 0) return '还没选中间片段'
    if (ending.length === 0) return '还没选结尾片段'
    if (!outputDir) return '还没选输出目录'
    if (count > permutationLimit) {
      return `中间选了 ${middle.length} 条，最多只能排出 ${permutationLimit} 种不同顺序`
    }
    return ''
  }, [opening, middle, ending, outputDir, count, permutationLimit])

  /** 提交任务 */
  const submit = async () => {
    const created = await runner.submit({
      opening,
      middle,
      ending,
      count,
      output_dir: outputDir,
    })
    if (created) {
      message.success(`任务 #${created.id} 已创建，开始混剪`)
    }
  }

  const cancelJob = async (jobId: number) => {
    if (await runner.cancel(jobId)) {
      message.success('任务已取消')
    }
  }

  const removeJob = async (jobId: number) => {
    if (await runner.remove(jobId)) {
      message.success('记录已删除（成片文件保留在磁盘上）')
    }
  }

  const openHistoryDetail = async (jobId: number) => {
    const detail = await runner.read(jobId, '加载任务详情失败')
    if (detail) {
      setHistoryDetail(detail)
    }
  }

  const job = runner.job

  // ---------- 渲染：选片弹窗 ----------
  /**
   * 给某一段挑素材的弹窗。
   *
   * 目录切换、目录增删、筛选、预览、勾选都收在这里 —— 页面上只有这一个地方
   * 跟「素材从哪来」相关，用户不需要在大网格和三段列表之间来回找。
   */
  const renderMaterialPicker = () => {
    if (pickerSection === null) {
      return null
    }
    const meta = SECTION_META[pickerSection]
    const ordered = pickerSection !== 'middle'
    const current = sources.find((source) => source.id === pickSourceId) ?? sources[0]

    const clipsOfSource = current ? clips.filter((clip) => clip.source_id === current.id) : []
    const text = pickKeyword.trim().toLowerCase()
    const filtered = text
      ? clipsOfSource.filter(
          (clip) =>
            clip.name.toLowerCase().includes(text) || clip.rel_path.toLowerCase().includes(text),
        )
      : clipsOfSource

    return (
      <Modal
        open
        width={880}
        title={`给「${meta.title}」添加素材`}
        onCancel={() => setPickerSection(null)}
        footer={
          <Flex justify="space-between" align="center" wrap gap={8}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              已选 <Text strong>{pickSelected.length}</Text> 条
              {ordered && pickSelected.length > 1 && '（勾选的先后顺序就是拼接顺序）'}
            </Text>
            <Space>
              <Button onClick={() => setPickerSection(null)}>取消</Button>
              <Button type="primary" disabled={pickSelected.length === 0} onClick={confirmPick}>
                加入 {pickSelected.length} 条
              </Button>
            </Space>
          </Flex>
        }
      >
        {library.loading && !library.data ? (
          <Flex justify="center" style={{ padding: 32 }}>
            <Spin />
          </Flex>
        ) : library.error ? (
          <Alert type="error" message="素材加载失败" description={library.error} showIcon />
        ) : sources.length === 0 ? (
          <Flex vertical gap={12} align="center" style={{ padding: '24px 0' }}>
            <Empty
              description={
                <>
                  还没有素材目录。添加一个本地目录 ——
                  镜头分割的切片、原片、别的工程导出的视频都能用。
                </>
              }
            />
            <Button
              type="primary"
              ghost
              loading={addingSource}
              onClick={() => picker.open('source')}
            >
              添加目录
            </Button>
          </Flex>
        ) : (
          <Flex vertical gap={8}>
            <Flex gap={8} wrap align="center">
              <Select
                value={current?.id}
                onChange={setPickSourceId}
                style={{ minWidth: 260 }}
                options={sources.map((source) => ({
                  value: source.id,
                  label: `${source.name}（${source.clip_count}）`,
                }))}
              />
              <Input
                allowClear
                placeholder="筛选文件名"
                value={pickKeyword}
                onChange={(event) => setPickKeyword(event.target.value)}
                style={{ width: 180 }}
              />
              <Button loading={addingSource} onClick={() => picker.open('source')}>
                添加目录
              </Button>
              <Button onClick={() => void library.reload()} loading={library.loading}>
                重新扫描
              </Button>
              <Popconfirm
                title="从素材列表移除这个目录？"
                description={`该目录的 ${current?.clip_count ?? 0} 条素材会从三个列表里一并移除；磁盘上的文件不会被删除。`}
                onConfirm={() => current && void handleRemoveSource(current.id)}
              >
                <Button type="text" danger disabled={!current}>
                  移除本目录
                </Button>
              </Popconfirm>
            </Flex>

            <Text type="secondary" style={{ fontSize: 12, wordBreak: 'break-all' }}>
              <Text code style={{ fontSize: 12 }}>
                {current?.path}
              </Text>
              {current?.exists === false && (
                <Tag color="error" style={{ marginLeft: 8 }}>
                  目录不存在（可能已删除或磁盘未挂载）
                </Tag>
              )}
              {current?.truncated && (
                <Tag color="warning" style={{ marginLeft: 8 }}>
                  素材过多，只收录了前 {current.clip_count} 条
                </Tag>
              )}
            </Text>

            {filtered.length === 0 ? (
              <Empty
                description={
                  clipsOfSource.length === 0 ? '这个目录下没有视频文件' : '没有匹配的素材'
                }
              />
            ) : (
              <>
                {filtered.length > MAX_RENDERED_CLIPS && (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    共 {filtered.length} 条，只显示前 {MAX_RENDERED_CLIPS} 条 ——
                    用上面的筛选框缩小范围
                  </Text>
                )}
                <div
                  style={{
                    display: 'grid',
                    gridTemplateColumns: 'repeat(auto-fill, minmax(132px, 1fr))',
                    gap: 8,
                    maxHeight: 420,
                    overflowY: 'auto',
                  }}
                >
                  {filtered.slice(0, MAX_RENDERED_CLIPS).map((clip) => {
                    const pickedAt = pickSelected.indexOf(clip.id)
                    const picked = pickedAt >= 0
                    return (
                      <Card
                        key={clip.id}
                        hoverable
                        onClick={() => togglePick(clip.id)}
                        styles={{ body: { padding: 6 } }}
                        style={{
                          borderColor: picked ? PICKED_BORDER : undefined,
                          borderWidth: picked ? 2 : 1,
                        }}
                        cover={
                          <div
                            style={{
                              position: 'relative',
                              height: 84,
                              background: '#000',
                              display: 'flex',
                              alignItems: 'center',
                              justifyContent: 'center',
                              overflow: 'hidden',
                            }}
                          >
                            <Image
                              src={clip.thumb_url}
                              alt={clip.name}
                              preview={false}
                              width="100%"
                              height={84}
                              style={{ objectFit: 'cover' }}
                              fallback={THUMB_FALLBACK}
                            />
                            {picked && (
                              <span style={PICKED_BADGE}>
                                {ordered ? pickedAt + 1 : <CheckOutlined />}
                              </span>
                            )}
                            {/* 播放角标：点它只看不选，别把手势和勾选搅在一起 */}
                            <span
                              style={PLAY_BADGE}
                              onClick={(event) => {
                                event.stopPropagation()
                                setPreviewClip(clip)
                              }}
                            >
                              <PlayCircleOutlined />
                            </span>
                          </div>
                        }
                      >
                        <Flex vertical gap={2}>
                          {/* 文件名完整显示，长了就换行 —— 素材名自带序号后缀，
                              截断后几条看起来一模一样，根本分不清选了哪条 */}
                          <Tooltip title={clip.rel_path}>
                            <Text style={{ fontSize: 12, wordBreak: 'break-all' }}>
                              {clip.name}
                            </Text>
                          </Tooltip>
                          <Text type="secondary" style={{ fontSize: 11 }}>
                            {formatDuration(clip.duration)}
                          </Text>
                        </Flex>
                      </Card>
                    )
                  })}
                </div>
              </>
            )}
          </Flex>
        )}
      </Modal>
    )
  }

  // ---------- 渲染：三段编排 ----------
  /**
   * 一段的编排卡片。
   *
   * 开头/结尾是有序列表（序号、↑↓、✕ 都在这儿），中间那一堆没有顺序可调 ——
   * 合成时才随机打乱，所以只给「移除」和「清空」。
   */
  const renderSection = (key: SectionKey) => {
    const [list] = sectionState[key]
    const meta = SECTION_META[key]
    const ordered = key !== 'middle'
    const totalDuration = list.reduce(
      (sum, clipId) => sum + (clipMap.get(clipId)?.duration ?? 0),
      0,
    )

    return (
      <Card
        // 三张卡片同行等宽：flex 1 + minWidth 0 才能在长文件名面前乖乖收缩
        style={{ flex: 1, minWidth: 0 }}
        title={
          <Space size={8} wrap>
            <Tag color={meta.color}>{meta.title}</Tag>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {meta.hint}
            </Text>
            {list.length > 0 && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                共 {list.length} 条 · {formatDuration(totalDuration)}
              </Text>
            )}
          </Space>
        }
        extra={
          key === 'middle' && list.length > 0 ? (
            <Button type="text" danger onClick={() => setMiddle([])}>
              清空
            </Button>
          ) : null
        }
      >
        {list.length === 0 ? (
          <Text type="secondary" style={{ fontSize: 12 }}>
            还没有选素材。点下面的「+ 添加素材」
            {ordered ? '，勾选的先后顺序就是拼接顺序。' : '，合成时会随机打乱它们的顺序。'}
          </Text>
        ) : ordered ? (
          <Flex vertical gap={8}>
            {list.map((clipId, index) => {
              const clip = clipMap.get(clipId)
              return (
                <Flex key={clipId} align="flex-start" gap={8}>
                  <Text
                    type="secondary"
                    style={{ fontSize: 12, flex: 'none', lineHeight: '40px', width: 16 }}
                  >
                    {index + 1}.
                  </Text>
                  <ClipThumb
                    clip={clip}
                    width={72}
                    onClick={clip ? () => setPreviewClip(clip) : undefined}
                  />
                  {/* 文件名完整显示（长了换行），时长挪到它下面 —— 横向空间全留给名字 */}
                  <Flex vertical gap={0} style={{ flex: 1, minWidth: 0 }}>
                    <Tooltip title={clip?.rel_path ?? '这个片段已经不在素材库里了'}>
                      <Text style={{ fontSize: 12, wordBreak: 'break-all' }}>
                        {clip?.name ?? `已失效的片段（${clipId}）`}
                      </Text>
                    </Tooltip>
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      {formatDuration(clip?.duration ?? null)}
                    </Text>
                  </Flex>
                  {/* 三个控件挤在一起且不许压缩：卡片的横向空间优先给文件名 */}
                  <Flex gap={0} style={{ flex: 'none' }}>
                    <Button
                      type="text"
                      icon={<ArrowUpOutlined />}
                      aria-label="上移"
                      disabled={index === 0}
                      onClick={() => moveIn(key, index, -1)}
                    />
                    <Button
                      type="text"
                      icon={<ArrowDownOutlined />}
                      aria-label="下移"
                      disabled={index === list.length - 1}
                      onClick={() => moveIn(key, index, 1)}
                    />
                    <Button
                      type="text"
                      danger
                      icon={<CloseOutlined />}
                      aria-label="移除"
                      onClick={() => removeFrom(key, index)}
                    />
                  </Flex>
                </Flex>
              )
            })}
          </Flex>
        ) : (
          <Flex wrap gap={8}>
            {list.map((clipId, index) => {
              const clip = clipMap.get(clipId)
              return (
                <Flex key={clipId} vertical gap={2} style={{ width: 96 }}>
                  <ClipThumb
                    clip={clip}
                    width={96}
                    height={56}
                    onClick={clip ? () => setPreviewClip(clip) : undefined}
                  />
                  <Tooltip title={clip?.rel_path ?? '这个片段已经不在素材库里了'}>
                    <Text style={{ fontSize: 11, wordBreak: 'break-all' }}>
                      {clip?.name ?? '已失效'}
                    </Text>
                  </Tooltip>
                  <Button type="text" danger block onClick={() => removeFrom(key, index)}>
                    移除
                  </Button>
                </Flex>
              )
            })}
          </Flex>
        )}
        <Button type="dashed" block style={{ marginTop: 8 }} onClick={() => openPicker(key)}>
          + 添加素材
        </Button>
      </Card>
    )
  }

  // ---------- 渲染：进度 ----------
  const renderProgress = () => {
    if (!job || isTerminalStatus(job.status)) {
      return null
    }
    const phaseText =
      job.current_phase === 'normalize'
        ? `正在归一化第 ${Math.min(job.done_clips + 1, job.total_clips)}/${job.total_clips} 个片段${job.current_clip ? `：${job.current_clip}` : ''}`
        : job.current_phase === 'concat'
          ? `正在拼接第 ${job.current_index}/${job.total_outputs} 条成片`
          : '排队等待中…'
    // 阶段内百分比：归一化阶段用片段数折算，拼接阶段用成片数折算 —— 分母都真实存在
    const percent =
      job.current_phase === 'normalize' && job.total_clips > 0
        ? Math.round(((job.done_clips + job.progress_percent / 100) / job.total_clips) * 100)
        : job.current_phase === 'concat' && job.total_outputs > 0
          ? Math.round(
              ((job.completed_outputs + job.progress_percent / 100) / job.total_outputs) * 100,
            )
          : 0
    return (
      <Card title={`任务 #${job.id} 进行中`}>
        <Flex vertical gap={8}>
          <Text>{phaseText}</Text>
          <Progress percent={Math.min(percent, 100)} status="active" />
        </Flex>
      </Card>
    )
  }

  // ---------- 渲染：成品 ----------
  const renderOutputs = (target: MixJob) => {
    if (target.outputs.length === 0) {
      return null
    }
    return (
      <Flex vertical gap={12}>
        <Title level={5} style={{ margin: 0 }}>
          成片（{target.completed_outputs}/{target.total_outputs}）
        </Title>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
            gap: 12,
          }}
        >
          {target.outputs.map((output) => (
            <Card
              key={output.id}
              styles={{ body: { padding: 8 } }}
              cover={
                output.status === 'success' ? (
                  <div
                    style={{
                      position: 'relative',
                      background: '#000',
                      height: 160,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      overflow: 'hidden',
                      cursor: 'pointer',
                    }}
                    onClick={() => setPreviewOutput(output)}
                  >
                    <Image
                      src={output.thumb_url}
                      alt={output.output_name}
                      preview={false}
                      height={160}
                      style={{ objectFit: 'cover' }}
                      fallback={THUMB_FALLBACK}
                    />
                    <span style={PLAY_BADGE}>
                      <PlayCircleOutlined />
                    </span>
                  </div>
                ) : undefined
              }
            >
              <Flex vertical gap={4}>
                <Flex justify="space-between" align="center">
                  <Text strong style={{ fontSize: 13 }}>
                    第 {output.index} 条
                  </Text>
                  <Tag color={OUTPUT_STATUS_META[output.status].color}>
                    {OUTPUT_STATUS_META[output.status].label}
                  </Tag>
                </Flex>
                {output.status === 'success' ? (
                  <>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {formatDuration(output.duration_seconds)} · {formatBytes(output.size_bytes)}
                    </Text>
                    <Tooltip title={output.output_path}>
                      <Text type="secondary" style={{ fontSize: 11 }} ellipsis>
                        {output.output_path}
                      </Text>
                    </Tooltip>
                  </>
                ) : (
                  output.error_message && (
                    <Tooltip title={output.error_message}>
                      <Text type="danger" style={{ fontSize: 12 }} ellipsis>
                        {output.error_message.split('\n')[0]}
                      </Text>
                    </Tooltip>
                  )
                )}

                {/* 这条成片到底拼了哪些片段、什么顺序 —— 多条成片的差别只在中间段，
                    不摆出来就没法核对「随机打乱」到底打乱成什么样 */}
                {output.order.length > 0 && (
                  <Flex vertical gap={4}>
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      顺序（{output.order.length} 段）
                    </Text>
                    <Flex gap={8} wrap align="flex-end">
                      {splitOrder(target, output.order)
                        .filter((group) => group.items.length > 0)
                        // flex: none + maxWidth 100%：整组算一整块 —— 卡片窄的时候
                        // 「中间」这一组整体落到下一行，而不是把它压成贴边的窄条；
                        // 组内格子仍然 28px 一格，排不下就自己在组内换行
                        .map((group) => (
                          <Flex
                            key={group.key}
                            vertical
                            gap={2}
                            style={{ flex: 'none', maxWidth: '100%' }}
                          >
                            <Flex gap={2} wrap>
                              {group.items.map((item) => (
                                <OrderThumb
                                  key={item.index}
                                  clip={findClipByPath(item.path)}
                                  rawPath={item.path}
                                  index={item.index + 1}
                                  color={SECTION_META[group.key].hex}
                                  onClick={() => setOrderDetail({ job: target, output })}
                                />
                              ))}
                            </Flex>
                            <Text style={{ fontSize: 10, color: SECTION_META[group.key].hex }}>
                              {SECTION_META[group.key].title} {group.items.length}
                            </Text>
                          </Flex>
                        ))}
                    </Flex>
                  </Flex>
                )}
              </Flex>
            </Card>
          ))}
        </div>
      </Flex>
    )
  }

  // ---------- 渲染：一条成片的完整拼接顺序 ----------
  const renderOrderDetail = () => {
    if (orderDetail === null) {
      return null
    }
    const { job: owner, output } = orderDetail
    return (
      <Modal
        open
        width={620}
        footer={null}
        onCancel={() => setOrderDetail(null)}
        destroyOnHidden
        title={`第 ${output.index} 条的拼接顺序（${output.order.length} 段）`}
      >
        <Flex vertical gap={8}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            开头 {owner.opening.length} 段（按你选的顺序）→ 中间 {owner.middle.length} 段
            {owner.middle.length > 1 ? '（每条成片顺序不同）' : ''}→ 结尾 {owner.ending.length} 段
          </Text>
          <div style={{ maxHeight: '58vh', overflowY: 'auto' }}>
            <Flex vertical gap={6}>
              {splitOrder(owner, output.order).flatMap((group) =>
                group.items.map((item, positionInGroup) => {
                  const clip = findClipByPath(item.path)
                  const fileName = item.path.split('/').pop() ?? item.path
                  return (
                    <Flex key={item.index} align="center" gap={8}>
                      <Text
                        style={{
                          fontSize: 11,
                          flex: 'none',
                          width: 46,
                          color: SECTION_META[group.key].hex,
                        }}
                      >
                        {SECTION_META[group.key].title} {positionInGroup + 1}
                      </Text>
                      <ClipThumb
                        clip={clip}
                        width={56}
                        height={32}
                        onClick={clip ? () => setPreviewClip(clip) : undefined}
                      />
                      <Flex vertical gap={0} style={{ flex: 1, minWidth: 0 }}>
                        <Tooltip title={item.path}>
                          <Text style={{ fontSize: 12, wordBreak: 'break-all' }}>
                            {clip?.name ?? fileName}
                          </Text>
                        </Tooltip>
                        {!clip && (
                          <Text type="secondary" style={{ fontSize: 11 }}>
                            素材已不在素材库里（目录可能被移除）
                          </Text>
                        )}
                      </Flex>
                      <Text type="secondary" style={{ fontSize: 11, flex: 'none' }}>
                        {formatDuration(clip?.duration ?? null)}
                      </Text>
                    </Flex>
                  )
                }),
              )}
            </Flex>
          </div>
        </Flex>
      </Modal>
    )
  }

  // ---------- 渲染：历史任务 ----------
  const historyColumns = (): ColumnsType<MixJob> => [
    {
      title: 'ID',
      dataIndex: 'id',
      width: 70,
      render: (id: number) => `#${id}`,
    },
    jobStatusColumn<MixJob>(JOB_STATUS_META),
    {
      title: '成片',
      key: 'outputs',
      width: 110,
      render: (_: unknown, record: MixJob) =>
        `${record.completed_outputs}/${record.total_outputs} 条` +
        (record.failed_outputs > 0 ? `（${record.failed_outputs} 失败）` : ''),
    },
    {
      title: '规模',
      key: 'scale',
      width: 130,
      render: (_: unknown, record: MixJob) =>
        `开头${record.opening.length} + 中间${record.middle.length} + 结尾${record.ending.length}`,
    },
    {
      title: '输出目录',
      dataIndex: 'output_dir',
      ellipsis: true,
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      width: 170,
      render: (value: string) => new Date(value).toLocaleString(),
    },
    {
      title: '操作',
      key: 'actions',
      width: 190,
      render: (_: unknown, record: MixJob) => (
        <Space size={4}>
          <Button type="link" onClick={() => void openHistoryDetail(record.id)}>
            详情
          </Button>
          {!isTerminalStatus(record.status) && (
            <Popconfirm title="确定取消该任务？" onConfirm={() => void cancelJob(record.id)}>
              <Button type="link" danger>
                取消
              </Button>
            </Popconfirm>
          )}
          {isTerminalStatus(record.status) && (
            <Popconfirm
              title="删除任务记录？成片文件会保留在磁盘上。"
              onConfirm={() => void removeJob(record.id)}
            >
              <Button type="link" danger>
                删除
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  return (
    // 不自己加 padding：留白由布局层统一给（Layout.tsx 的 Content，28px 32px），
    // 这里再加一层就成了别处都没有的双重留白。
    // maxWidth 是为了三栏编排：屏幕很宽时不让三张卡片各自拉成巨幅。
    <Flex vertical gap={16} style={{ maxWidth: 1400, margin: '0 auto' }}>
      {contextHolder}
      <Title level={4} style={{ margin: 0 }}>
        智能混剪
      </Title>

      {env && !env.ready && (
        <Alert
          type="error"
          showIcon
          message="混剪依赖未就绪"
          description={
            <Flex vertical gap={4}>
              {env.dependencies
                .filter((dependency) => !dependency.ok)
                .map((dependency) => (
                  <Text key={dependency.name}>
                    {dependency.name}：{dependency.detail}
                    {dependency.fix_hint && <Text code>（{dependency.fix_hint}）</Text>}
                  </Text>
                ))}
            </Flex>
          }
        />
      )}

      {/* ---------- 三段编排（素材就在这三张卡片里各自挑选） ----------
          三张卡片必须同行：中间段是待打乱的集合，跟前后两段并排摆着才看得出
          「这是一条成片的三段」；一旦换行，中间段被挤到第二行就断了这个关系 */}
      <Flex gap={12} align="flex-start">
        {renderSection('opening')}
        {renderSection('middle')}
        {renderSection('ending')}
      </Flex>

      {/* ---------- 合成设置 ---------- */}
      <Card title="合成设置">
        <Flex gap={24} wrap="wrap" align="center">
          <Space>
            <Text>混剪条数</Text>
            <InputNumber
              min={1}
              max={Math.min(20, Math.max(permutationLimit, 1))}
              value={count}
              onChange={(v) => setCount(Math.max(1, Math.floor(v ?? 1)))}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {middle.length > 0
                ? `中间 ${middle.length} 条最多可排出 ${permutationLimit} 种不同顺序`
                : '选完中间片段后这里会显示可排出的顺序数'}
            </Text>
          </Space>
          <Space>
            <Text>输出目录</Text>
            <Text code style={{ fontSize: 12 }}>
              {outputDir || '未选择'}
            </Text>
            <Button onClick={() => picker.open('output')}>选择目录</Button>
          </Space>
          {estimate > 0 && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              单条预估时长 {formatDuration(Math.round(estimate))}（画幅跟开头第一条素材，其余等比缩放补黑边）
            </Text>
          )}
          <Button
            type="primary"
            disabled={Boolean(validationError) || runner.submitting || runner.running}
            loading={runner.submitting}
            onClick={() => void submit()}
          >
            {validationError || (count > 1 ? `开始混剪 ${count} 条` : '开始混剪')}
          </Button>
        </Flex>
      </Card>

      {/* ---------- 进度与成品 ---------- */}
      {renderProgress()}
      {job && (
        <Card
          title={
            <Space size={8}>
              <span>任务 #{job.id}</span>
              <Tag color={JOB_STATUS_META[job.status].color}>
                {JOB_STATUS_META[job.status].label}
              </Tag>
            </Space>
          }
          extra={
            !isTerminalStatus(job.status) ? (
              <Popconfirm title="确定取消该任务？" onConfirm={() => void cancelJob(job.id)}>
                <Button danger>取消</Button>
              </Popconfirm>
            ) : undefined
          }
        >
          {job.error_message && (
            <Alert
              type="warning"
              showIcon
              message={job.error_message}
              style={{ marginBottom: 12 }}
            />
          )}
          {renderOutputs(job)}
        </Card>
      )}

      {/* ---------- 历史任务 ---------- */}
      <HistoryCard
        columns={historyColumns()}
        dataSource={history.items}
        loading={history.loading}
        pagination={{
          total: history.total,
          pageSize: 10,
          showSizeChanger: false,
          current: history.page,
          onChange: history.setPage,
        }}
      />

      {/* ---------- 选片弹窗（哪一段的素材、从哪个目录挑，都在这里面） ---------- */}
      {renderMaterialPicker()}

      {/* ---------- 素材预览弹窗 ---------- */}
      <VideoPreviewModal
        open={previewClip !== null}
        title={previewClip?.name}
        src={previewClip?.video_url}
        videoKey={previewClip?.id}
        caption={
          previewClip && (
            // 素材可以来自任意目录，把绝对路径亮出来，便于确认它到底取自哪儿
            <Text type="secondary" style={{ fontSize: 12, wordBreak: 'break-all' }}>
              {previewClip.abs_path}
            </Text>
          )
        }
        onClose={() => setPreviewClip(null)}
      />

      {/* ---------- 成品预览弹窗 ---------- */}
      <VideoPreviewModal
        open={previewOutput !== null}
        title={previewOutput ? `第 ${previewOutput.index} 条成片` : ''}
        src={previewOutput?.video_url}
        videoKey={previewOutput?.id}
        onClose={() => setPreviewOutput(null)}
      />

      {/* ---------- 成片拼接顺序弹窗 ---------- */}
      {renderOrderDetail()}

      {/* ---------- 历史任务详情弹窗 ---------- */}
      <Modal
        open={historyDetail !== null}
        title={
          historyDetail && (
            <Space size={8}>
              <span>任务 #{historyDetail.id}</span>
              <Tag color={JOB_STATUS_META[historyDetail.status].color}>
                {JOB_STATUS_META[historyDetail.status].label}
              </Tag>
            </Space>
          )
        }
        footer={null}
        width={860}
        onCancel={() => setHistoryDetail(null)}
      >
        {historyDetail && (
          <Flex vertical gap={8}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              输出目录：<Text code style={{ fontSize: 12 }}>{historyDetail.output_dir}</Text>
            </Text>
            {historyDetail.error_message && (
              <Alert type="warning" showIcon message={historyDetail.error_message} />
            )}
            {renderOutputs(historyDetail)}
          </Flex>
        )}
      </Modal>

      {/* ---------- 目录选择器 ---------- */}
      <DirectoryPicker
        open={picker.active === 'output'}
        title="选择输出目录"
        initialPath={outputDir || env?.default_output_dir}
        onClose={picker.close}
        onSelect={(path) => {
          setOutputDir(path)
          picker.close()
        }}
      />
      {/* 素材目录选择器：从选片弹窗里唤起，选完自动切到那个目录 */}
      <DirectoryPicker
        open={picker.active === 'source'}
        title="选择素材目录"
        initialPath={env?.default_source_dir}
        onClose={picker.close}
        onSelect={(path) => void handleAddSource(path)}
      />
    </Flex>
  )
}
