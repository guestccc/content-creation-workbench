/**
 * 一键换背景相关的类型定义。
 *
 * 字段命名与后端 API 保持一致（snake_case），不做 camelCase 转换。
 *
 * 这里只放类型与「与类型强相关的常量」（默认参数、状态展示配置），
 * 不放工具函数 —— 格式化一律走 utils/format.ts。
 */

/** 任务状态 */
export type BackgroundJobStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'partial'
  | 'failed'
  | 'cancelled'

/** 单张原图的处理状态 */
export type BackgroundJobItemStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'skipped'

/**
 * 算法参数。默认值全部等于 `抠图.py` 的命令行默认值。
 *
 * 字段含义与每一步在堵什么漏见后端 services/cutout.py 的模块说明 ——
 * 这里只做类型，不重复解释算法。
 */
export interface BackgroundJobParams {
  // ---------- 抠图 ----------
  /** 纸的透明线（相对纸–墨跨度）：亮度高于此比例的像素判为纯纸 */
  hi_frac: number
  /** 墨的实心线：亮度低于此比例的像素判为纯墨 */
  lo_frac: number
  /** 主体定位线档位；null = auto（扫档位取包围盒炸开前那一档） */
  dark_frac: number | null
  /** 纸纹截断：低于此透明度的淡灰直接归零 */
  pedestal: number
  /** 位置门：主体包围盒之外整片置零 */
  gate: boolean
  /** 位置门往外扩的比例 */
  gate_pad: number
  /** 暖色抑制：按彩度压掉主体旁边的木纹 / 暖色桌面 */
  warm: boolean
  /** 保留原始像素颜色（默认统一成墨色，避免深色底上出白边） */
  keep_color: boolean

  // ---------- 贴合 ----------
  /** 整幅画布（含透明留白）宽度占底图宽度的比例（默认 0.8）；null = 不缩放，原尺寸原位置居中贴。任何情况下都不裁边 */
  scale: number | null
  /** 贴合位置：关键字或 "x,y" 坐标；null = center */
  pos: string | null
  /** 自动落点的搜索起点（距页顶的比例），pos=auto 时生效 */
  search_from: number
  /** 贴纸离底图边缘的最小距离（像素） */
  margin: number
  /** 贴纸旋转角度（度） */
  rotate: number
  /** 贴纸不透明度 0–1 */
  opacity: number
  /** 校准红框：贴纸描 2px 红边再贴，用来看实际落位与尺寸（验证原尺寸居中时打开） */
  debug_border: boolean
}

/**
 * 诊断统计。
 *
 * 这个算法的失败模式（定位线把背景圈进来、整张蒙雾）光看产物图判不出来，
 * 这组数字是排查的主要依据，所以它的字段都在界面上显示。
 * 键由算法侧决定，新增字段不该让前端编译不过，因此留了索引签名。
 */
export interface BackgroundStats {
  /** 纸面亮度 */
  paper?: number
  /** 墨芯亮度 */
  ink_luma?: number
  /** 纸墨跨度；太小时 warnings 里会有提示 */
  span?: number
  /** 线性映射的两条线 */
  hi?: number
  lo?: number
  /** 实际生效的定位线及其档位说明 */
  dark?: number
  dark_frac?: number
  dark_auto?: boolean
  dark_note?: string
  dark_line?: string
  /** 主体包围盒 [x0, y0, x1, y1] 及其可读形式 */
  bbox?: number[]
  bbox_text?: string
  /** 位置门开关与扩边像素 */
  gate?: boolean
  gate_pad?: number
  /** 暖色抑制开关与它抠掉的像素数 */
  warm?: boolean
  warm_cut?: number
  /** 统一后的墨色 */
  ink_rgb?: number[]
  /** 画布 / 背景 / 产物 / 贴纸 / 落点 */
  canvas?: number[]
  page?: number[]
  output?: number[]
  /** 贴上去的画布尺寸 = 原图画布（scale=null）或整体缩放后的画布；永不裁边 */
  sticker?: number[]
  position?: number[]
  /** 贴合时用的缩放比例；null = 原尺寸贴 */
  scale?: number | null
  /** 这张产物带没带校准红框（排查「为什么图上有红边」先看这里） */
  debug_border?: boolean
  /** 是否走了「输入已经是透明底」的直通分支 */
  already_transparent?: boolean
  /** 不透明像素数 / 可见像素数 / 可见占比 */
  opaque_px?: number
  visible_px?: number
  visible_ratio?: number
  /** 异常情况的提示（跨度太小、包围盒过大等） */
  warnings?: string[]
  [key: string]: unknown
}

/** 任务中单张原图的处理结果 */
export interface BackgroundJobItem {
  id: number
  index: number
  source_path: string
  source_name: string
  output_path: string
  output_name: string
  status: BackgroundJobItemStatus
  stats: BackgroundStats
  width: number
  height: number
  size_bytes: number
  elapsed_seconds: number
  error_message: string
  started_at: string | null
  finished_at: string | null
}

/** 换背景任务 */
export interface BackgroundJob {
  id: number
  status: BackgroundJobStatus
  input_path: string
  output_dir: string
  /** 勾选的原图文件名清单；为空表示处理目录下全部图片 */
  files: string[]
  /** 来源素材抓取任务 id；为空表示不是从抓取结果带过来的（仅溯源，弱关联） */
  source_crawl_job_id: number | null
  /** 来源笔记 id（空串表示没有来源）。素材抓取页靠它把任务挂回笔记行 */
  source_crawl_note_id: string
  background_path: string
  background_name: string
  params: BackgroundJobParams
  total_images: number
  completed_images: number
  failed_images: number
  skipped_images: number
  current_index: number
  current_image: string
  /** 当前这张已跑的秒数。抠图没有中间进度可上报，只能如实显示已用时 */
  current_elapsed_seconds: number
  progress_percent: number
  started_at: string | null
  finished_at: string | null
  error_message: string
  /** 备注（用户可编辑，最多 200 字；空串表示没写） */
  remark: string
  created_at: string
  updated_at: string
  /** 每张原图的处理结果；列表接口不返回 */
  items: BackgroundJobItem[]
}

/** 任务列表数据 */
export interface BackgroundJobListData {
  total: number
  page: number
  page_size: number
  items: BackgroundJob[]
}

/** 创建任务的请求体 */
export interface BackgroundJobPayload {
  input_path: string
  background_path: string
  output_dir?: string
  /** 只处理目录下的这些文件名；留空表示全部图片 */
  files?: string[]
  /** 来源素材抓取任务 id；带来源时才传（后端会校验这个任务存在） */
  source_crawl_job_id?: number
  /** 来源笔记 id；与上面的任务 id 成对出现，不能只给这一个 */
  source_crawl_note_id?: string
  params: BackgroundJobParams
}

/**
 * 从素材抓取带进来的一批原图。
 *
 * 那边一条笔记的图已经在本地同一个目录里，正好对应这里「一个目录 + 勾选一批
 * 文件」的形态。两种来路：进页面前由路由 state 带进来，或者在页面里用
 * 「从素材抓取选图」现挑一条笔记。
 */
export interface BackgroundSwapPrefill {
  /** 原图目录的绝对路径 */
  inputPath: string
  /** 目录里要勾选的文件名（纯文件名，不带路径） */
  files: string[]
  /** 来源说明，展示用（如「素材抓取任务 #12 · 保温杯测评」） */
  source: string
  /** 来源抓取任务 id；不是从抓取来的就没有这一项，建任务时也不带来源 */
  crawlJobId?: number
  /** 来源笔记 id；与 crawlJobId 成对出现 */
  crawlNoteId?: string
}

/**
 * 跳进换背景页时挂在路由 state 上的载荷（react-router location.state）。
 *
 * 两种用途可以同时出现（比如「带一批图进来并打开某条任务的详情弹窗」），
 * 所以做成一个对象而不是「state 本身就是 prefill」——后者两种用途只能二选一。
 */
export interface BackgroundSwapEntry {
  /** 带上来的原图（目录 + 勾选 + 来源说明） */
  prefill?: BackgroundSwapPrefill
  /** 进页面就直接打开这条换背景任务的详情弹窗（素材抓取那边「看详情」用） */
  openJobId?: number
}

/**
 * 算法参数的默认值（与后端 schemas/background_job.py 一致）。
 *
 * 除 `scale` 外都等于脚本默认。`scale` 默认 0.8 = 整幅画布（永不裁边）整体缩到
 * 背景宽的 80% —— 原尺寸贴大背景只占三成宽，偏小；清空输入框即回原尺寸原位置贴。
 */
export const DEFAULT_BACKGROUND_PARAMS: BackgroundJobParams = {
  hi_frac: 0.9,
  lo_frac: 0.15,
  dark_frac: null,
  pedestal: 0.08,
  gate: true,
  gate_pad: 0.12,
  warm: true,
  keep_color: false,
  scale: 0.8,
  pos: null,
  search_from: 0.35,
  margin: 20,
  rotate: 0.0,
  opacity: 1.0,
  debug_border: false,
}

/** 贴合位置候选项；空值 = 居中（后端默认） */
export const POS_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: '居中' },
  { value: 'auto', label: '自动找落点' },
  { value: 'tl', label: '左上' },
  { value: 'tr', label: '右上' },
  { value: 'bl', label: '左下' },
  { value: 'br', label: '右下' },
]

/** 状态展示配置：中文标签与 antd Tag 颜色 */
export const JOB_STATUS_META: Record<
  BackgroundJobStatus,
  { label: string; color: string; hint: string }
> = {
  pending: { label: '排队中', color: 'default', hint: '等待后台开始处理' },
  running: { label: '进行中', color: 'processing', hint: '正在逐张抠图换背景' },
  success: { label: '已完成', color: 'success', hint: '全部图片处理成功' },
  partial: { label: '部分成功', color: 'warning', hint: '有图片失败，成功的产物已保留' },
  failed: { label: '失败', color: 'error', hint: '全部图片都失败了' },
  cancelled: { label: '已取消', color: 'default', hint: '任务被手动取消' },
}

/** 条目状态展示配置 */
export const ITEM_STATUS_META: Record<
  BackgroundJobItemStatus,
  { label: string; color: string }
> = {
  pending: { label: '等待中', color: 'default' },
  running: { label: '处理中', color: 'processing' },
  success: { label: '成功', color: 'success' },
  failed: { label: '失败', color: 'error' },
  skipped: { label: '已跳过', color: 'default' },
}

/** 判断任务是否已结束（用于停止轮询） */
export function isTerminalStatus(status: BackgroundJobStatus): boolean {
  return status === 'success' || status === 'partial' || status === 'failed' || status === 'cancelled'
}
