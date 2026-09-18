/**
 * 视频字幕提取相关的类型定义。
 *
 * 字段命名与后端 API 保持一致（snake_case），不做 camelCase 转换。
 */

/** 任务状态 */
export type SubtitleJobStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'partial'
  | 'failed'
  | 'cancelled'

/** 单条视频的处理状态 */
export type SubtitleJobItemStatus = 'pending' | 'running' | 'success' | 'failed' | 'skipped'

/** 任务中单条视频的处理结果 */
export interface SubtitleJobItem {
  id: number
  index: number
  source_path: string
  source_name: string
  output_path: string
  output_name: string
  status: SubtitleJobItemStatus
  subtitle_exists: boolean
  file_size: number
  segment_count: number
  duration_seconds: number | null
  exit_code: number | null
  elapsed_seconds: number
  error_message: string
  started_at: string | null
  finished_at: string | null
}

/** 字幕提取任务 */
export interface SubtitleJob {
  id: number
  status: SubtitleJobStatus
  input_path: string
  output_dir: string
  recursive: boolean
  params: Record<string, unknown> & {
    asr?: string
    language?: string
    format?: string
  }
  total_videos: number
  completed_videos: number
  failed_videos: number
  skipped_videos: number
  subtitle_count: number
  current_index: number
  current_video: string
  /** 当前这条已跑的秒数。转写没有机器可读的百分比（VideoCaptioner 的进度条
   *  只在 stderr 是终端时才渲染），所以这是唯一能如实给出的实时数字 */
  current_elapsed_seconds: number
  /** 总进度百分比：按视频条数计算（服务端算好） */
  progress_percent: number
  started_at: string | null
  finished_at: string | null
  error_message: string
  created_at: string
  updated_at: string
  /** 每条视频的处理结果；列表接口不返回 */
  items: SubtitleJobItem[]
}

/** 任务列表数据 */
export interface SubtitleJobListData {
  total: number
  page: number
  page_size: number
  items: SubtitleJob[]
}

/** 一个 ASR 引擎选项（真源在后端） */
export interface AsrEngine {
  key: string
  name: string
  summary: string
  requires_key: boolean
  recommended: boolean
}

/** 一条安装指引；命令只是文本，只用于展示与复制 */
export interface InstallHint {
  title: string
  command: string
  note: string
  url: string
}

/** 环境自检结果 */
export interface SubtitleEnvironment {
  installed: boolean
  /** 能否开始转写：装了 VideoCaptioner 且 ffmpeg 可用 */
  ready: boolean
  launcher: string[]
  kind: string
  root: string
  version: string
  python_version: string
  config_file: string
  config_exists: boolean
  ffmpeg_path: string
  detail: string
  platform: string
  platform_label: string
  python_platform: string
  materials_dir: string
  /** 默认输入目录：materials/source */
  default_input_dir: string
  /** 默认输出目录：materials/subtitle */
  default_output_dir: string
  install_hints: InstallHint[]
  asr_engines: AsrEngine[]
}

/** 一份产出的字幕文件 */
export interface SubtitleFile {
  index: number
  item_index: number
  name: string
  source_name: string
  size_bytes: number
  segment_count: number
}

/** 一份字幕文件的文本内容（预览用，可能被截断） */
export interface SubtitleText {
  index: number
  name: string
  source_name: string
  content: string
  size_bytes: number
  truncated: boolean
}

/** 创建任务的请求体 */
export interface SubtitleJobPayload {
  input_path: string
  output_dir?: string
  asr?: string
  /** 识别语言（ISO 639-1），留空表示自动检测 */
  language?: string
  recursive?: boolean
  /** 只处理目录下的这些文件名；留空表示全部 */
  files?: string[]
}

/** 状态展示配置：中文标签与 antd Tag 颜色 */
export const JOB_STATUS_META: Record<
  SubtitleJobStatus,
  { label: string; color: string; hint: string }
> = {
  pending: { label: '排队中', color: 'default', hint: '等待后台开始处理' },
  running: { label: '进行中', color: 'processing', hint: '正在转写' },
  success: { label: '已完成', color: 'success', hint: '全部视频转写成功' },
  partial: { label: '部分成功', color: 'warning', hint: '有视频失败，已产出的字幕已保留' },
  failed: { label: '失败', color: 'error', hint: '全部视频都失败了' },
  cancelled: { label: '已取消', color: 'default', hint: '任务被手动取消' },
}

/** 条目状态展示配置 */
export const ITEM_STATUS_META: Record<
  SubtitleJobItemStatus,
  { label: string; color: string }
> = {
  pending: { label: '等待中', color: 'default' },
  running: { label: '转写中', color: 'processing' },
  success: { label: '成功', color: 'success' },
  failed: { label: '失败', color: 'error' },
  skipped: { label: '已跳过', color: 'default' },
}

/** 判断任务是否已结束（用于停止轮询） */
export function isTerminalStatus(status: SubtitleJobStatus): boolean {
  return status === 'success' || status === 'partial' || status === 'failed' || status === 'cancelled'
}

/** 把秒数格式化为 mm:ss */
export function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
}

/** 格式化字节数 */
export function formatBytes(bytes: number): string {
  if (bytes <= 0) {
    return '—'
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(0)} KB`
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
