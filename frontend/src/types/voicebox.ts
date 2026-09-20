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

  // ---- 页面按钮（「设置镜像」「重启 Voicebox」）的可用性 ----
  // 这八个字段只为回答「按钮该不该出现、该说什么话」。**前端不做平台判断**：
  // 系统差异、下载源现状、安装位置全由后端算好，页面只按这些值渲染。
  /** 后端所在系统：macos / windows / linux */
  platform: string
  /** 系统名的中文展示 */
  platform_label: string
  /** 能否由本工具设置模型下载源（系统支持且 Voicebox 就在本机） */
  hf_mirror_supported: boolean
  /** 当前 HF_ENDPOINT 的值；空串表示未设置 */
  hf_mirror_value: string
  /** 当前下载源是不是本页推荐的镜像 */
  hf_mirror_is_recommended: boolean
  /** 下载源在注销 / 重启后是否仍然有效 */
  hf_mirror_persistent: boolean
  /** 找到的 Voicebox 安装位置；空串表示没找到 */
  voicebox_app_path: string
  /** 页面是否该显示「重启 Voicebox」 */
  restart_supported: boolean
}

/** 一次「重启 Voicebox」的结果（**不代表服务已就绪**，就绪靠轮询自检判断） */
export interface VoiceboxRestartResult {
  /** 是否已发起启动 */
  started: boolean
  /** 拉起的安装位置；空串表示按名字查找拉起 */
  app_path: string
  /** 给用户看的一句话 */
  detail: string
  /** 要等多久 / 等的是什么 */
  wait_hint: string
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

/** 一个能用来配音的模型（= 上游 engine + model_size 的一个组合） */
export interface DubbingModel {
  /** 上游 /generate 的 engine 参数 */
  engine: string
  /** 上游 /generate 的 model_size 参数；**空串表示不分尺寸**，提交时这个字段不发 */
  model_size: string
  /** 上游 /models/status 里的标识 */
  model_name: string
  /** 页面上显示的名字 */
  label: string
  /** 一句话说明它适合什么场景 */
  note: string
  /** 是否已下载；**null 表示上游列表里没这个模型（可能是版本差异），不是「没下载」** */
  downloaded: boolean | null
  /** 是否正在下载 */
  downloading: boolean
  /** 是否已加载进显存/内存 */
  loaded: boolean
  /** 已下载时占用的体积（MB） */
  size_mb: number | null
}

/** 配音可选模型清单（顺序即后端给的顺序，页面照排） */
export interface DubbingModelListData {
  items: DubbingModel[]
  total: number
}

/**
 * 模型在下拉里的唯一键。
 *
 * 为什么不用 model_name：那是**上游**的标识，上游改了名它就变了，而页面的选中值
 * 不该随之漂移。engine + model_size 才是我们跟上游约定的调用参数，也是提交时要
 * 发的东西 —— 用同一对值做键，选中值可以直接拿去提交，不必再查一次表。
 */
export function modelKey(model: Pick<DubbingModel, 'engine' | 'model_size'>): string {
  return `${model.engine}:${model.model_size}`
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
  /** 这次用的 TTS 引擎 */
  engine: string
  /** 这次用的模型规模；空串表示这个引擎不分尺寸 */
  model_size: string
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
  /** TTS 引擎与模型规模（一起指向模型列表里的一个模型） */
  engine?: string
  /** 部分引擎不分尺寸，此时传空串（后端会整个不发这个字段） */
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

/**
 * 语言候选。**上游支持的远不止这两个**（它收了二十几种），这里只放开中英文：
 * 本工具的产出是中文短视频，别的语言没人用，而且多语言能力还取决于所选 engine
 * （Kokoro 就是英文为主）。
 */
export const LANGUAGE_OPTIONS = [
  { value: 'zh', label: '中文' },
  { value: 'en', label: '英文' },
] as const

// 模型候选**不再写死在这里** —— 它跟着所装的 Voicebox 版本走（哪些下好了、有哪些
// 引擎都只有上游知道），改由 GET /voicebox/models 下发，见 useDubbingModels。

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
