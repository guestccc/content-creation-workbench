/**
 * 智能配音（Voicebox）相关的类型定义。
 *
 * 字段命名与后端 API 保持一致（snake_case），不做 camelCase 转换。
 *
 * 与其它功能的一处结构差异：**没有任务表**。后端只做 Voicebox 的 HTTP 代理，
 * 生成记录活在进程内存里（`id` 是自增 int，重启即清空），产物落在磁盘上
 * （materials/dubbing/），所以「历史」就是产物清单，不是任务列表。
 */

/** 服务地址来自哪一层：环境变量 / .env / 内置默认 */
export type VoiceboxBaseUrlSource = 'environment' | 'env_file' | 'default'

/** Voicebox 的运行环境自检结果 */
export interface VoiceboxEnvironment {
  /** 能不能开始生成：服务连得上且模型不是「未下载」 */
  ready: boolean
  /** 后端实际调用的服务地址 */
  base_url: string
  base_url_source: VoiceboxBaseUrlSource
  /** 服务连得上吗（连不上不是错误，是「桌面端没开」这个正常状态） */
  reachable: boolean
  status: string
  model_loaded: boolean
  /** 上游拿不到这个信息时为 null */
  model_downloaded: boolean | null
  model_size: string | null
  gpu_available: boolean
  vram_used_mb: number | null
  /** Voicebox 里已有的音色数量 */
  profile_count: number
  /** 配音产物默认落盘目录（materials/dubbing） */
  default_output_dir: string
  /** 一句话说明当前状态或失败原因 */
  detail: string
  /** 未就绪时的下一步动作 */
  fix_hint: string
  /** 连不上时的分步指引（纯文本，后端不代装） */
  install_hints: VoiceboxInstallHint[]
  /** 需要提醒用户的情况（没显卡、模型没下完、没有音色……） */
  warnings: string[]
}

/** 一条安装/启动指引 */
export interface VoiceboxInstallHint {
  title: string
  /** 要执行的命令；为空表示这一步没有命令 */
  command: string
  note: string
  url: string
}

/** 一个音色（建音色在 Voicebox 自己的界面里做） */
export interface VoiceProfile {
  id: string
  name: string
  description: string | null
  language: string
}

/** 音色列表 */
export interface VoiceProfileListData {
  items: VoiceProfile[]
  total: number
}

/** 一次配音生成的状态 */
export type DubbingStatus = 'queued' | 'running' | 'success' | 'failed'

/** 一次配音生成的进度记录（只活在服务端内存里，重启即空） */
export interface DubbingGeneration {
  id: number
  status: DubbingStatus
  /** 文案摘要（后端截断，轮询不必每 1.5 秒传一遍全文） */
  text_excerpt: string
  profile_id: string
  profile_name: string
  /** 产物文件名（含扩展名），成功后有值 */
  filename: string
  output_path: string
  duration: number | null
  error_message: string
  /** 当前阶段的一句话说明 */
  progress_hint: string
  created_at: string | null
  finished_at: string | null
  elapsed_seconds: number
}

/** 提交一次配音生成（后端入队后立即返回） */
export interface DubbingGenerationPayload {
  text: string
  profile_id: string
  /** 音色名，只用于记录与展示 */
  profile_name?: string
  /** 产物文件名（不含扩展名）；留空按 dub_<时间戳> 自动命名 */
  filename?: string
  language?: string
  model_size?: string
}

/** materials/dubbing/ 下的一份配音产物 */
export interface DubbingAudio {
  name: string
  /** 音频流地址（后端给全，前端不拼） */
  audio_url: string
  size_bytes: number
  /** 时长（秒）；索引里没有则为 null（不现场探测） */
  duration: number | null
  profile_name: string
  text_excerpt: string
  created_at: string | null
  /** false 表示这个文件不是本功能生成的（用户自己放进目录的） */
  indexed: boolean
}

/** 配音产物清单 */
export interface DubbingAudioListData {
  items: DubbingAudio[]
  total: number
  dir: string
}

/** 语言候选：与上游 GenerationRequest 的 `^(en|zh)$` 对齐 */
export const LANGUAGE_OPTIONS = [
  { value: 'zh', label: '中文' },
  { value: 'en', label: '英文' },
] as const

/** 模型规模候选：与上游的 `^(1\.7B|0\.6B)$` 对齐 */
export const MODEL_SIZE_OPTIONS = [
  { value: '1.7B', label: '1.7B（质量更好，更慢）' },
  { value: '0.6B', label: '0.6B（更快，质量一般）' },
] as const

/** 文案字数上限：与后端 VOICEBOX_MAX_TEXT_CHARS / 上游 maxLength 对齐 */
export const MAX_TEXT_CHARS = 5000

/** 生成状态展示配置 */
export const GENERATION_STATUS_META: Record<
  DubbingStatus,
  { label: string; color: string; hint: string }
> = {
  queued: { label: '排队中', color: 'default', hint: 'Voicebox 一次只跑一条，马上开始' },
  running: { label: '生成中', color: 'processing', hint: '正在合成语音' },
  success: { label: '已完成', color: 'success', hint: '产物已落到 materials/dubbing/' },
  failed: { label: '失败', color: 'error', hint: '看下面的失败原因' },
}

/** 判断生成是否已结束（用于停止轮询） */
export function isGenerationTerminal(status: DubbingStatus): boolean {
  return status === 'success' || status === 'failed'
}
