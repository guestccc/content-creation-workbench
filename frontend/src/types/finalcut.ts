/**
 * 一键成品（finalcut）相关的类型定义。
 *
 * 字段命名与后端 API 保持一致（snake_case），不做 camelCase 转换。
 * 两个任务域：copy-jobs（AI 文案生成，无磁盘产物）与 render-jobs（烧字合成，
 * 每条候选文案一个成片）。
 *
 * **render-jobs 那一半是「仍在的后端接口的镜像」**：烧录已从页面上摘掉（改为
 * 人工在剪映烧字），但后端路由/表/服务原样保留，所以这里的类型与
 * `api/finalcut.ts` 的包装函数也不删 —— 删了要恢复就得重抄一遍。页面侧唯一
 * 的差别是少了使用点（`ITEM_STATUS_META` 现在就是这样一个孤儿）。
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

/**
 * 一条候选广告文案。
 *
 * 没有 `target_seconds` 了：那是模型自己回填的、恒等于视频时长的一个假数字
 * （跟它写的字数毫无关系）。「这条念出来几秒」现在由页面用 `char_count ÷ 语速`
 * 实时算（见 `pages/Finalcut/copyLength.ts`）。老任务的 result 里可能还残留着
 * 这个键，后端会静默丢掉，不影响打开。
 */
export interface CopyCandidate {
  text: string
  angle: string
  /** 口播字数（服务端实测：含标点、不含换行） */
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
  /**
   * 本次任务实际生效的口播语速（字/秒），创建时快照落库 —— 历史任务的
   * 「约念几秒」与字数预算都按它算，改全局配置不会动到它。
   * 0 = 升级前创建的老任务（当时按全局值跑的，具体值不可考），展示成「—」。
   */
  chars_per_second: number
  copy_count: number
  hint: string
  model: string
  result: CopyResultPayload | null
  tokens_used: number
  current_phase: CopyPhase
  progress_percent: number
  error_message: string
  /** 备注（用户可编辑，最多 200 字；空串表示没写） */
  remark: string
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
}

/**
 * 一条文案任务用的字幕内容（第 ② 步左右对照：左列看字幕，右列看 AI 文案）。
 *
 * 两个视图来自同一次读盘：`content` 是磁盘上那份字幕的原文，`material` 是
 * **喂给 AI 的素材**（去序号/时间轴、合并连续重复行、剥 ASS 标签、按上限截断）。
 * 后者别用前端的文本变换去凑 —— 两者不等价。
 */
export interface FinalcutCopySubtitleText {
  /** 字幕文件绝对路径（后端从任务记录里取的，不接受前端传入） */
  path: string
  name: string
  size_bytes: number
  /** 字幕原文（超上限时只给开头一段） */
  content: string
  /** 原文是否被截断（文件超过预览上限） */
  truncated: boolean
  /** 喂给 AI 的素材文本 */
  material: string
  /** 素材是否被截断（超过素材字数上限，AI 当时也只读到了这些） */
  material_truncated: boolean
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
  /** 备注（用户可编辑，最多 200 字；空串表示没写） */
  remark: string
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
  /** 口播语速（字/秒，1.0–15.0）；不传 = 后端按当前全局默认值快照 */
  chars_per_second?: number
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

/**
 * 环境自检结果。
 *
 * `ready` 是「烧字环境」的判据（AI + ffmpeg-drawtext + 中文字体三样齐全），
 * 页面摘掉烧录后**不再用它**报警 —— 页面上剩下的文案生成只认 `ai.ok`
 * （本机 brew 的 ffmpeg 没有 drawtext，`ready` 会恒为 false 挂着一条无用警告）。
 * `ready`/`ffmpeg`/`font`/`warnings` 照旧由后端给（接口保留），类型也不删。
 */
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
  /** 口播语速（字/秒）：页面算「约念几秒」与字数预算的依据 */
  chars_per_second: number
  warnings: string[]
}

/** 「AI 配置与口播语速」弹窗的读取模型（key 只给掩码） */
export interface AiSettings {
  base_url: string
  model: string
  api_key_present: boolean
  api_key_masked: string
  shadowed_keys: string[]
  warning: string
  /** 当前生效的口播语速（字/秒） */
  chars_per_second: number
  /** 内置默认值（弹窗的「恢复默认」用） */
  chars_per_second_default: number
  /** 环境变量顶着这个键时的提示（空串 = 没被顶） */
  chars_per_second_warning: string
}

/**
 * 保存 AI 配置与语速的请求体。
 *
 * `api_key` 留空 = 保持原值；`chars_per_second` **不传 = 不改**（两者语义一致）。
 * 语速的区间校验在后端（1.0–15.0），越界 400。
 */
export interface AiSettingsPayload {
  base_url: string
  model: string
  api_key: string
  chars_per_second?: number
}

/** 一条历史产物来源（选素材步骤的清单条目） */
export interface FinalcutSource {
  path: string
  name: string
  origin: 'mix' | 'subtitle'
  job_id: number
  /** 条目在来源任务内的序号（从 1 开始）—— 字幕预览按「任务 + 序号」取内容 */
  index: number
  /** 来源任务的备注（空串表示没写）—— 产物表没有这列，是 join 任务表带出来的 */
  remark: string
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

/**
 * 文案任务的状态展示配置：中文标签与 antd Tag 颜色。
 *
 * 类型收窄到 `CopyJobStatus`（原先挂 `RenderJobStatus`）：合成任务的 `partial`
 * 在文案任务里不存在，挂在上面只会诱导页面写永远不成立的分支。合成任务那套
 * 若日后回到页面，按上面的表加一份即可。
 */
export const JOB_STATUS_META: Record<
  CopyJobStatus,
  { label: string; color: string; hint: string }
> = {
  pending: { label: '排队中', color: 'default', hint: '等待后台开始处理' },
  running: { label: '进行中', color: 'processing', hint: '正在生成文案' },
  success: { label: '已完成', color: 'success', hint: '文案已生成' },
  failed: { label: '失败', color: 'error', hint: '任务失败，原因见详情' },
  cancelled: { label: '已取消', color: 'default', hint: '任务被手动取消' },
}

/**
 * 单条成片状态展示配置。
 *
 * **当前页面已无使用点**（烧录第 ③ 步从页面摘掉了，改为人工在剪映烧字）；
 * 保留是因为后端 render 接口仍在，页面恢复时要用。`RenderStep.tsx` 已删。
 */
export const ITEM_STATUS_META: Record<RenderItemStatus, { label: string; color: string }> = {
  pending: { label: '等待中', color: 'default' },
  running: { label: '合成中', color: 'processing' },
  success: { label: '成功', color: 'success' },
  failed: { label: '失败', color: 'error' },
  skipped: { label: '已跳过', color: 'default' },
}

/** 判断文案任务是否已结束（用于停止轮询） */
export function isTerminalStatus(status: CopyJobStatus): boolean {
  return status === 'success' || status === 'failed' || status === 'cancelled'
}

/** 文案任务相位的中文名（进度展示用） */
export const COPY_PHASE_LABEL: Record<Exclude<CopyPhase, ''>, string> = {
  read: '读取字幕素材',
  analyze: '调用 AI 生成文案',
  parse: '解析生成结果',
}
