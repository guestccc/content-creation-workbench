/**
 * 一键成品（finalcut）相关的类型定义。
 *
 * 字段命名与后端 API 保持一致（snake_case），不做 camelCase 转换。
 * 两个任务域：copy-jobs（AI 文案生成，无磁盘产物）与 render-jobs（烧字合成，
 * 每条候选文案一个成片）。
 */

/** 文案任务状态 */
export type CopyJobStatus = 'pending' | 'running' | 'success' | 'failed' | 'cancelled'

/** 合成任务状态（比文案任务多一个 partial：部分成片失败） */
export type RenderJobStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'partial'
  | 'failed'
  | 'cancelled'

/** 单条成片的状态 */
export type RenderItemStatus = 'pending' | 'running' | 'success' | 'failed' | 'skipped'

/** 文案任务相位：读字幕 → 调 AI → 解析结果 */
export type CopyPhase = '' | 'read' | 'analyze' | 'parse'

/** 上屏样式模板 key（与后端 finalcut_render.TEXT_STYLES 一致） */
export type TextStyleKey = 'white_box' | 'yellow' | 'outline'

/** 视频画面上的框选区域（归一化 0-1，相对显示尺寸） */
export interface BoxSpec {
  x: number
  y: number
  w: number
  h: number
}

/** AI 对字幕素材的拆解（生成文案的论据） */
export interface CopyAnalysis {
  topic: string
  audience: string
  selling_points: string[]
  tone: string
}

/** 一条候选文案的逐段拆解中的一段 */
export interface CopyBreakdownPart {
  part: string
  content: string
  explain: string
}

/** 一条候选广告文案 */
export interface CopyCandidate {
  text: string
  angle: string
  target_seconds: number
  char_count: number
  why: string
  highlights: string[]
  breakdown: CopyBreakdownPart[]
}

/** 一次文案生成的完整产物 */
export interface CopyResultPayload {
  analysis: CopyAnalysis
  copies: CopyCandidate[]
}

/** 文案生成任务 */
export interface FinalcutCopyJob {
  id: number
  status: CopyJobStatus
  subtitle_path: string
  video_path: string
  video_duration: number
  copy_count: number
  hint: string
  model: string
  result: CopyResultPayload | null
  tokens_used: number
  current_phase: CopyPhase
  progress_percent: number
  error_message: string
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
}

/** 合成任务中的一条成片 */
export interface FinalcutRenderItem {
  id: number
  index: number
  status: RenderItemStatus
  copy_text: string
  angle: string
  style: string
  box: BoxSpec
  font_size: number
  resolved_font_size: number
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

/** 合成任务 */
export interface FinalcutRenderJob {
  id: number
  status: RenderJobStatus
  copy_job_id: number | null
  video_path: string
  video_duration: number
  video_spec: {
    width?: number
    height?: number
    fps_num?: number
    fps_den?: number
    has_audio?: boolean
    audio_codec?: string
  }
  output_dir: string
  font_file: string
  total_items: number
  completed_items: number
  failed_items: number
  skipped_items: number
  current_index: number
  progress_percent: number
  error_message: string
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
  /** 详情接口才有明细；列表接口恒为空数组 */
  items: FinalcutRenderItem[]
}

export interface FinalcutCopyJobListData {
  total: number
  page: number
  page_size: number
  items: FinalcutCopyJob[]
}

export interface FinalcutRenderJobListData {
  total: number
  page: number
  page_size: number
  items: FinalcutRenderJob[]
}

/** 创建文案任务的请求体 */
export interface CopyJobPayload {
  subtitle_path: string
  video_path: string
  copy_count?: number
  hint?: string
}

/** 合成任务里一条的请求形状（文案快照 × 框 × 样式） */
export interface RenderItemPayload {
  copy_text: string
  angle?: string
  style?: string
  box: BoxSpec
  font_size?: number
}

/** 创建合成任务的请求体 */
export interface RenderJobPayload {
  video_path: string
  copy_job_id?: number
  items: RenderItemPayload[]
  output_dir?: string
}

/** 环境自检里的 AI 一项 */
export interface FinalcutAiStatus {
  configured: boolean
  ok: boolean
  base_url: string
  model: string
  key_present: boolean
  key_masked: string
  detail: string
  fix_hint: string
}

/** 环境自检里的 ffmpeg-drawtext 一项 */
export interface FinalcutFfmpegStatus {
  ok: boolean
  path: string
  version: string
  has_drawtext: boolean
  supports_boxborderw: boolean
  detail: string
  fix_hint: string
}

/** 一套上屏样式的展示形态（渲染色块用） */
export interface FinalcutTextStyleItem {
  key: TextStyleKey
  label: string
  preview_text: string
  preview_background: string
  default: boolean
}

/** 环境自检结果 */
export interface FinalcutEnvironment {
  ready: boolean
  ai: FinalcutAiStatus
  ffmpeg: FinalcutFfmpegStatus
  font: { file: string; family: string } | null
  default_output_dir: string
  text_styles: FinalcutTextStyleItem[]
  default_style: TextStyleKey
  copy_count_default: number
  copy_count_max: number
  max_items: number
  warnings: string[]
}

/** 「AI 配置」弹窗的读取模型（key 只给掩码） */
export interface AiSettings {
  base_url: string
  model: string
  api_key_present: boolean
  api_key_masked: string
  shadowed_keys: string[]
  warning: string
}

/** 保存 AI 配置的请求体；api_key 留空 = 保持原值 */
export interface AiSettingsPayload {
  base_url: string
  model: string
  api_key: string
}

/** 一条历史产物来源（选素材步骤的清单条目） */
export interface FinalcutSource {
  path: string
  name: string
  origin: 'mix' | 'subtitle'
  job_id: number
  duration_seconds: number | null
  size_bytes: number
  video_url: string
  thumb_url: string
}

/** 历史产物来源清单 */
export interface FinalcutSources {
  subtitles: FinalcutSource[]
  videos: FinalcutSource[]
}

/** 状态展示配置：中文标签与 antd Tag 颜色（文案任务与合成任务共用） */
export const JOB_STATUS_META: Record<
  RenderJobStatus,
  { label: string; color: string; hint: string }
> = {
  pending: { label: '排队中', color: 'default', hint: '等待后台开始处理' },
  running: { label: '进行中', color: 'processing', hint: '正在生成/合成' },
  success: { label: '已完成', color: 'success', hint: '全部完成' },
  partial: { label: '部分成功', color: 'warning', hint: '有成片失败，成功的已保留' },
  failed: { label: '失败', color: 'error', hint: '任务失败，原因见详情' },
  cancelled: { label: '已取消', color: 'default', hint: '任务被手动取消' },
}

/** 单条成片状态展示配置 */
export const ITEM_STATUS_META: Record<RenderItemStatus, { label: string; color: string }> = {
  pending: { label: '等待中', color: 'default' },
  running: { label: '合成中', color: 'processing' },
  success: { label: '成功', color: 'success' },
  failed: { label: '失败', color: 'error' },
  skipped: { label: '已跳过', color: 'default' },
}

/** 判断任务是否已结束（用于停止轮询） */
export function isTerminalStatus(status: CopyJobStatus | RenderJobStatus): boolean {
  return status === 'success' || status === 'partial' || status === 'failed' || status === 'cancelled'
}

/** 文案任务相位的中文名（进度展示用） */
export const COPY_PHASE_LABEL: Record<Exclude<CopyPhase, ''>, string> = {
  read: '读取字幕素材',
  analyze: '调用 AI 生成文案',
  parse: '解析生成结果',
}
