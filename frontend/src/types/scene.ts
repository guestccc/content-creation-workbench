/**
 * 智能镜头分割相关的类型定义。
 *
 * 字段命名与后端 API 保持一致（snake_case），不做 camelCase 转换。
 */

/** 任务模式：preview 只检测切点，split 真正切出片段 */
export type SceneJobMode = 'preview' | 'split'

/** 任务状态 */
export type SceneJobStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'partial'
  | 'failed'
  | 'cancelled'

/** 单个视频的处理状态 */
export type SceneJobItemStatus = 'pending' | 'running' | 'success' | 'failed' | 'skipped'

/** 一个切点（一个镜头） */
export interface Scene {
  number: number
  start: number
  end: number
  duration: number
}

/** 任务中单个视频的处理结果 */
export interface SceneJobItem {
  id: number
  index: number
  source_path: string
  source_name: string
  output_dir: string
  status: SceneJobItemStatus
  scene_count: number
  scenes: Scene[] | null
  clip_count: number
  clip_names: string[]
  failed_clip_count: number
  /** 单镜头视频：切不出片段是正常结果，界面上要说明而不是显示成失败 */
  single_shot: boolean
  duration_seconds: number | null
  exit_code: number | null
  elapsed_seconds: number
  error_message: string
  started_at: string | null
  finished_at: string | null
}

/** 镜头分割任务 */
export interface SceneJob {
  id: number
  mode: SceneJobMode
  status: SceneJobStatus
  input_path: string
  output_dir: string
  recursive: boolean
  params: Record<string, unknown> & {
    detector?: string
    threshold?: number | null
    min_len?: number
    copy?: boolean
  }
  total_videos: number
  completed_videos: number
  failed_videos: number
  skipped_videos: number
  clip_count: number
  scene_count: number
  current_index: number
  current_video: string
  /** 当前视频已处理到的片段序号（vct 逐段上报，失败/跳过的也往前走） */
  current_clips: number
  current_clip_names: string[]
  /** 当前视频所处阶段：detect 检测中 / split 切割中；空串表示还没收到上报 */
  current_phase: '' | 'detect' | 'split'
  /** 当前视频预计切出的片段数。检测跑完才知道分母，之前恒为 0（那时只能显示「检测中」） */
  current_total_clips: number
  /** 总进度百分比：按视频条数计算（服务端算好） */
  progress_percent: number
  started_at: string | null
  finished_at: string | null
  error_message: string
  created_at: string
  updated_at: string
  /** 每个视频的处理结果；列表接口不返回 */
  items: SceneJobItem[]
}

/** 任务列表数据 */
export interface SceneJobListData {
  total: number
  page: number
  page_size: number
  items: SceneJob[]
}

/** 模板 */
export interface SceneTemplate {
  key: string
  name: string
  summary: string
  best_for: string
  detector: string
  detector_label: string
  threshold: number | null
  threshold_label: string
  min_len: number
  copy_mode: boolean
  recommended: boolean
}

/** 依赖自检明细 */
export interface DependencyStatus {
  name: string
  ok: boolean
  path: string
  detail: string
  fix_hint: string
}

/** 环境自检结果 */
export interface SceneEnvironment {
  ready: boolean
  vct_path: string
  vct_exists: boolean
  /** 素材目录根（仓库根目录的 materials/），下有 source/clips/subtitle/output 四个分段 */
  materials_dir: string
  /** 默认输入目录：materials/source，页面用它作为输入目录的初始值 */
  default_input_dir: string
  /** 默认输出目录：materials/clips，页面用它作为输出目录的初始值 */
  default_output_dir: string
  dependencies: DependencyStatus[]
}

/** 切分产出的片段 */
export interface SceneClip {
  /** 片段序号（全任务范围内连续）；缩略图与播放接口用它定位 */
  index: number
  /** 所属视频在任务内的序号；按它把片段归到各条视频下，不靠文件名猜 */
  item_index: number
  name: string
  source_name: string
  size_bytes: number
  thumb_url: string
  /** 视频流地址（后端支持 Range，可拖动进度条）；路径由后端给，前端不拼 */
  video_url: string
}

/** 预览切点汇总 */
export interface SceneSummary {
  job_id: number
  status: SceneJobStatus
  total_scenes: number
  total_duration: number
  shortest: number | null
  longest: number | null
  average: number | null
  items: SceneJobItem[]
}

/** 创建任务的请求体 */
export interface SceneJobPayload {
  input_path: string
  output_dir?: string
  mode: SceneJobMode
  template?: string
  detector?: string
  threshold?: number | null
  min_len?: number
  /** 直接复制流不重编码：快且无损，但片段起点只能落在关键帧上 */
  copy?: boolean
  recursive?: boolean
  /** 只处理目录下的这些文件名；留空表示全部 */
  files?: string[]
}

/** 目录中的一项 */
export interface FsEntry {
  name: string
  path: string
  is_dir: boolean
  is_video: boolean
  size_bytes: number | null
}

/** 列目录结果 */
export interface FsListData {
  path: string
  parent: string | null
  entries: FsEntry[]
  truncated: boolean
  video_count: number
}

/** 检测器候选项（与后端 core/scene_templates.py 的 DETECTORS 对应） */
export const DETECTOR_OPTIONS: { value: string; label: string }[] = [
  { value: 'adaptive', label: '自适应（抗运镜、抗闪光）' },
  { value: 'content', label: '内容（标准快切，对画面变化更敏感）' },
  { value: 'threshold', label: '阈值（淡入淡出、切黑场）' },
]

/** 状态展示配置：中文标签与 antd Tag 颜色 */
export const JOB_STATUS_META: Record<
  SceneJobStatus,
  { label: string; color: string; hint: string }
> = {
  pending: { label: '排队中', color: 'default', hint: '等待后台开始处理' },
  running: { label: '进行中', color: 'processing', hint: '正在检测或切割' },
  success: { label: '已完成', color: 'success', hint: '全部视频处理成功' },
  partial: { label: '部分成功', color: 'warning', hint: '有视频失败，成功的片段已保留' },
  failed: { label: '失败', color: 'error', hint: '全部视频都失败了' },
  cancelled: { label: '已取消', color: 'default', hint: '任务被手动取消' },
}

/** 条目状态展示配置 */
export const ITEM_STATUS_META: Record<
  SceneJobItemStatus,
  { label: string; color: string }
> = {
  pending: { label: '等待中', color: 'default' },
  running: { label: '处理中', color: 'processing' },
  success: { label: '成功', color: 'success' },
  failed: { label: '失败', color: 'error' },
  skipped: { label: '已跳过', color: 'default' },
}

/** 判断任务是否已结束（用于停止轮询） */
export function isTerminalStatus(status: SceneJobStatus): boolean {
  return status === 'success' || status === 'partial' || status === 'failed' || status === 'cancelled'
}

/** 格式化字节数，用于片段卡片展示文件大小 */
export function formatBytes(bytes: number): string {
  if (bytes <= 0) {
    return '—'
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(0)} KB`
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 把秒数格式化为 mm:ss */
export function formatDuration(seconds: number | null): string {
  if (seconds === null || seconds <= 0) {
    return '--:--'
  }
  const total = Math.round(seconds)
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
}
