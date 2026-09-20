/**
 * 智能混剪相关的类型定义。
 *
 * 字段命名与后端 API 保持一致（snake_case），不做 camelCase 转换。
 */

/** 任务状态 */
export type MixJobStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'partial'
  | 'failed'
  | 'cancelled'

/** 单条成片的状态 */
export type MixOutputStatus = 'pending' | 'running' | 'success' | 'failed' | 'skipped'

/** 任务当前阶段：normalize 归一化 / concat 拼接 */
export type MixPhase = '' | 'normalize' | 'concat'

/** 用户添加的一个素材目录 */
export interface MixSource {
  /** 目录 id（sha1(绝对路径)） */
  id: string
  /** 目录绝对路径 */
  path: string
  /** 目录名（展示用） */
  name: string
  /** 目录当前是否还在（被删或盘没挂上时为 false） */
  exists: boolean
  added_at: number
  /** 收录到的视频数（扫描结果才有） */
  clip_count: number
  /** 是否因超过单目录上限被截断 */
  truncated: boolean
}

/** 素材库中的一段视频 */
export interface MixClip {
  /** 片段 id（sha1(绝对路径)），接口参数用它而不是路径 */
  id: string
  name: string
  /** 所属子目录（相对素材目录，空串表示直接放在根下） */
  group: string
  /** 相对所属素材目录的路径 */
  rel_path: string
  /** 绝对路径（展示与排障用） */
  abs_path: string
  /** 所属素材目录 id */
  source_id: string
  duration: number | null
  size_bytes: number
  thumb_url: string
  /** 视频流地址（后端支持 Range，可拖动进度条） */
  video_url: string
}

/** 素材扫描结果 */
export interface MixLibraryData {
  /** 已添加的素材目录 */
  sources: MixSource[]
  clips: MixClip[]
  scanned_at: number
}

/** 依赖自检明细 */
export interface MixDependencyStatus {
  name: string
  ok: boolean
  path: string
  detail: string
  fix_hint: string
}

/** 环境自检结果 */
export interface MixEnvironment {
  ready: boolean
  materials_dir: string
  /** 「添加素材目录」选择器的默认起始位置 */
  default_source_dir: string
  default_output_dir: string
  dependencies: MixDependencyStatus[]
}

/** 任务中的一条成片 */
export interface MixOutputItem {
  id: number
  index: number
  /** 该条成片的完整最终顺序（素材绝对路径），复盘用 */
  order: string[]
  status: MixOutputStatus
  output_path: string
  output_name: string
  duration_seconds: number | null
  size_bytes: number
  exit_code: number | null
  elapsed_seconds: number
  error_message: string
  started_at: string | null
  finished_at: string | null
  thumb_url: string
  video_url: string
}

/** 混剪任务 */
export interface MixJob {
  id: number
  status: MixJobStatus
  opening: string[]
  middle: string[]
  ending: string[]
  count: number
  output_dir: string
  seed: number
  target: { width?: number; height?: number; fps_num?: number; fps_den?: number }
  total_outputs: number
  completed_outputs: number
  failed_outputs: number
  skipped_outputs: number
  total_clips: number
  done_clips: number
  current_index: number
  current_clip: string
  current_phase: MixPhase
  progress_percent: number
  started_at: string | null
  finished_at: string | null
  error_message: string
  /** 备注（用户可编辑，最多 200 字；空串表示没写） */
  remark: string
  created_at: string
  updated_at: string
  outputs: MixOutputItem[]
}

/** 任务列表数据 */
export interface MixJobListData {
  total: number
  page: number
  page_size: number
  items: MixJob[]
}

/** 创建任务的请求体 */
export interface MixJobPayload {
  opening: string[]
  middle: string[]
  ending: string[]
  count: number
  output_dir: string
}

/** 状态展示配置：中文标签与 antd Tag 颜色 */
export const JOB_STATUS_META: Record<
  MixJobStatus,
  { label: string; color: string; hint: string }
> = {
  pending: { label: '排队中', color: 'default', hint: '等待后台开始处理' },
  running: { label: '进行中', color: 'processing', hint: '正在归一化或拼接' },
  success: { label: '已完成', color: 'success', hint: '全部成片合成成功' },
  partial: { label: '部分成功', color: 'warning', hint: '有成片失败，成功的已保留' },
  failed: { label: '失败', color: 'error', hint: '全部成片都失败了' },
  cancelled: { label: '已取消', color: 'default', hint: '任务被手动取消' },
}

/** 成片状态展示配置 */
export const OUTPUT_STATUS_META: Record<
  MixOutputStatus,
  { label: string; color: string }
> = {
  pending: { label: '等待中', color: 'default' },
  running: { label: '拼接中', color: 'processing' },
  success: { label: '成功', color: 'success' },
  failed: { label: '失败', color: 'error' },
  skipped: { label: '已跳过', color: 'default' },
}

/** 判断任务是否已结束（用于停止轮询） */
export function isTerminalStatus(status: MixJobStatus): boolean {
  return status === 'success' || status === 'partial' || status === 'failed' || status === 'cancelled'
}

/** 中间段能排出的不同顺序数（K!），与后端 middle_permutation_limit 一致 */
export function middlePermutationLimit(middleCount: number): number {
  let result = 1
  for (let i = 2; i <= middleCount; i += 1) {
    result *= i
  }
  return result
}
