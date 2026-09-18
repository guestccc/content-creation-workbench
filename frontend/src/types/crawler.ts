/**
 * 素材抓取（MediaCrawler）相关的类型定义。
 *
 * 字段命名与后端 API 保持一致（snake_case），不做 camelCase 转换。
 */

/** 平台标识（后端 CrawlPlatform 枚举的字符串值） */
export type CrawlPlatform = 'xhs' | 'dy' | 'ks' | 'bili' | 'wb' | 'tieba' | 'zhihu'

/** 抓取模式 */
export type CrawlerType = 'search' | 'detail' | 'creator'

/** 登录方式（phone 登录依赖外部短信转发服务，不暴露） */
export type CrawlLoginType = 'qrcode' | 'cookie'

/** 任务状态（抓取一次成败分明，没有「部分成功」；取消/失败时已抓内容保留） */
export type CrawlJobStatus = 'pending' | 'running' | 'success' | 'failed' | 'cancelled'

/** 抓取任务 */
export interface CrawlJob {
  id: number
  status: CrawlJobStatus
  platform: CrawlPlatform
  platform_label: string
  crawler_type: CrawlerType
  login_type: CrawlLoginType
  /** 实际生效的抓取参数（不含 cookies —— 凭据只入库，任何响应都不回显） */
  params: Record<string, unknown> & {
    keywords?: string[]
    ids?: string[]
    creators?: string[]
  }
  output_dir: string
  /** 预估抓取总量（估算值：平台有最小每页数、结果有去重与风控截流） */
  expected_count: number
  /** 已落盘的笔记数（running 期间实时更新） */
  crawled_count: number
  /** 终态定稿的笔记数 */
  note_count: number
  elapsed_seconds: number
  /** 服务端算好：running 封顶 99，100 只留给终态 */
  progress_percent: number
  started_at: string | null
  finished_at: string | null
  error_message: string
  created_at: string
  updated_at: string
}

/** 一条归一化后的笔记/作品（跨平台字段已对齐） */
export interface CrawlNote {
  index: number
  id: string
  type: string
  title: string
  desc: string
  nickname: string
  /** MC 落盘为字符串（如 "6.6万"），原样透传 */
  liked_count: string
  comment_count: string
  share_count: string
  publish_time: string
  url: string
  cover: string
  images: string[]
  /** 相对任务输出目录的本地图片路径（走 /media 接口取） */
  local_images: string[]
  local_videos: string[]
  source_keyword: string
}

/** 一个平台在本机的登录态缓存情况 */
export interface PlatformLoginState {
  platform: string
  platform_label: string
  /** 是否有 CDP 模式的登录态目录 */
  cdp: boolean
  /** 是否有标准模式的登录态目录 */
  standard: boolean
  updated_at: string
}

/** 一条安装指引；命令只是文本，只用于展示与复制 */
export interface InstallHint {
  title: string
  command: string
  note: string
  url: string
}

/** 环境自检结果 */
export interface CrawlEnvironment {
  installed: boolean
  ready: boolean
  /** 后端实际调用的命令前缀（诊断用） */
  launcher: string[]
  /** 命中方式：venv-python / uv-run */
  kind: string
  mc_root: string
  python_version: string
  /** 未就绪时的原因说明 */
  detail: string
  /** Node.js 版本；为空表示没找到（抖音/知乎签名需要） */
  node_version: string
  node_required_platforms: string[]
  /** 本机已有登录态缓存的平台（扫码一次后不用再扫） */
  login_states: PlatformLoginState[]
  /** MC 是否开启了媒体文件下载（ENABLE_GET_MEIDAS） */
  media_enabled: boolean
  /** MC 的 --creator_id 是否已支持知乎（原版缺该分支，需打补丁） */
  zhihu_creator_cli_supported: boolean
  default_output_dir: string
  install_hints: InstallHint[]
  warnings: string[]
}

/** 任务列表数据（结构与 useJobList 的 PagedList 一致） */
export interface CrawlJobListData {
  total: number
  page: number
  page_size: number
  items: CrawlJob[]
}

/** 结果接口返回 */
export interface CrawlResultsData {
  total: number
  notes: CrawlNote[]
}

/** 日志接口返回 */
export interface CrawlLogData {
  log: string
}

/** 创建任务的请求体 */
export interface CrawlJobPayload {
  platform: CrawlPlatform
  crawler_type: CrawlerType
  login_type: CrawlLoginType
  /** search 模式必填 */
  keywords?: string[]
  /** detail 模式必填 */
  ids?: string[]
  /** creator 模式必填 */
  creators?: string[]
  /** login_type=cookie 时必填 */
  cookies?: string
  start_page?: number
  max_notes: number
  get_comments: boolean
  get_sub_comments: boolean
  max_comments: number
  /** 扫码登录必须关；qrcode 时前端强制传 false */
  headless?: boolean
  max_concurrency: number
}

/** 状态展示配置：中文标签与 antd Tag 颜色 */
export const JOB_STATUS_META: Record<
  CrawlJobStatus,
  { label: string; color: string; hint: string }
> = {
  pending: { label: '排队中', color: 'default', hint: '等待后台开始抓取（同时只跑一个任务）' },
  running: { label: '进行中', color: 'processing', hint: '正在抓取；进度按已落盘条数估算' },
  success: { label: '已完成', color: 'success', hint: '抓取结束，可查看结果' },
  failed: { label: '失败', color: 'error', hint: '抓取失败，可展开日志尾部排查' },
  cancelled: { label: '已取消', color: 'default', hint: '任务被手动取消，已抓内容保留' },
}

/** 平台展示配置 */
export const PLATFORM_META: Record<CrawlPlatform, { label: string; color: string }> = {
  xhs: { label: '小红书', color: 'red' },
  dy: { label: '抖音', color: 'volcano' },
  ks: { label: '快手', color: 'orange' },
  bili: { label: '哔哩哔哩', color: 'blue' },
  wb: { label: '微博', color: 'gold' },
  tieba: { label: '贴吧', color: 'cyan' },
  zhihu: { label: '知乎', color: 'geekblue' },
}

/** 模式展示配置 */
export const CRAWLER_TYPE_META: Record<CrawlerType, { label: string; color: string }> = {
  search: { label: '关键词搜索', color: 'blue' },
  detail: { label: '指定笔记', color: 'cyan' },
  creator: { label: '创作者主页', color: 'purple' },
}

/** 判断任务是否已结束（用于停止轮询） */
export function isTerminalStatus(status: CrawlJobStatus): boolean {
  return status === 'success' || status === 'failed' || status === 'cancelled'
}

/** 笔记展示标题：没标题的平台（如微博）用正文摘要兜底 */
export function noteDisplayTitle(note: Pick<CrawlNote, 'title' | 'desc'>): string {
  const title = note.title.trim()
  if (title) {
    return title
  }
  const desc = note.desc.trim().replace(/\s+/g, ' ')
  if (desc) {
    return desc.length > 50 ? `${desc.slice(0, 50)}…` : desc
  }
  return '（无标题）'
}

// ---------------------------------------------------------------------------
// 平台 × 模式能力矩阵
//
// 数据从 MediaCrawler 源码核实（cmd_arg/arg.py 与各平台 client），是素材抓取
// 表单联动与创作者主页表单提示的共用事实源：支持的模式、输入框提示、
// 每页最小条数、Node 依赖、媒体下载范围都从这里查，不在页面里写 if 链。
// ---------------------------------------------------------------------------

/** 该平台在媒体下载开启后实际能拿到的媒体类型 */
export type MediaSupport = 'image+video' | 'image' | 'video' | 'none'

export interface PlatformSpec {
  key: CrawlPlatform
  label: string
  /** 支持的抓取模式（bili / tieba 没有关键词搜索） */
  modes: CrawlerType[]
  /** search 每页最小条数：平台会强制抬升，低于它预估会失真；无 search 时无意义 */
  searchMinNotes: number
  /** detail 模式输入框的 placeholder 与说明 */
  detailPlaceholder: string
  detailHelp: string
  /** creator 模式输入框的 placeholder 与说明（创作者主页表单也用它） */
  creatorPlaceholder: string
  creatorHelp: string
  /** 签名依赖 Node.js（pyexecjs 跑 js） */
  needsNode: boolean
  media: MediaSupport
}

const SEARCH_MODES: CrawlerType[] = ['search', 'detail', 'creator']
const NO_SEARCH_MODES: CrawlerType[] = ['detail', 'creator']

export const PLATFORM_SPECS: Record<CrawlPlatform, PlatformSpec> = {
  xhs: {
    key: 'xhs',
    label: PLATFORM_META.xhs.label,
    modes: SEARCH_MODES,
    searchMinNotes: 20,
    detailPlaceholder: 'https://www.xiaohongshu.com/explore/xxxx?xsec_token=…（每行一条）',
    detailHelp: '必须用带 xsec_token 的完整分享链接，纯笔记 ID 拿不到数据',
    creatorPlaceholder: '创作者主页链接（带 xsec_token）或纯用户 ID，每行一条',
    creatorHelp: '主页链接同样需要 xsec_token 参数',
    needsNode: false,
    media: 'image+video',
  },
  dy: {
    key: 'dy',
    label: PLATFORM_META.dy.label,
    modes: SEARCH_MODES,
    searchMinNotes: 10,
    detailPlaceholder: '作品链接 / 分享短链 / modal_id / 纯数字作品 ID（每行一条）',
    detailHelp: '链接和 ID 都支持，短链会自动解析',
    creatorPlaceholder: '创作者主页链接或 sec_uid，每行一条',
    creatorHelp: '主页链接或纯 sec_uid 均可',
    needsNode: true,
    media: 'image+video',
  },
  ks: {
    key: 'ks',
    label: PLATFORM_META.ks.label,
    modes: SEARCH_MODES,
    searchMinNotes: 20,
    detailPlaceholder: '作品链接或纯视频 ID（每行一条）',
    detailHelp: '链接和 ID 都支持',
    creatorPlaceholder: '创作者主页链接或纯 user_id，每行一条',
    creatorHelp: '主页链接或纯 user_id 均可',
    needsNode: false,
    media: 'none',
  },
  bili: {
    key: 'bili',
    label: PLATFORM_META.bili.label,
    modes: NO_SEARCH_MODES,
    searchMinNotes: 0,
    detailPlaceholder: '视频链接或 BV 号（每行一条）',
    detailHelp: '完整链接或纯 BV 号均可',
    creatorPlaceholder: '空间主页链接或纯 UID，每行一条',
    creatorHelp: 'space.bilibili.com 链接或纯 UID 均可',
    needsNode: false,
    media: 'video',
  },
  wb: {
    key: 'wb',
    label: PLATFORM_META.wb.label,
    modes: SEARCH_MODES,
    searchMinNotes: 10,
    detailPlaceholder: '纯微博 ID（数字，每行一条）',
    detailHelp: '微博详情只认数字 ID，可在微博详情页 URL 里找',
    creatorPlaceholder: '纯用户 UID（数字），每行一条',
    creatorHelp: '只支持纯数字 UID，不支持主页链接',
    needsNode: false,
    media: 'image',
  },
  tieba: {
    key: 'tieba',
    label: PLATFORM_META.tieba.label,
    modes: NO_SEARCH_MODES,
    searchMinNotes: 0,
    detailPlaceholder: '帖子 ID 或 /p/xxxx 帖子链接（每行一条）',
    detailHelp: '链接会自动归一化成帖子 ID',
    creatorPlaceholder: '创作者主页链接或 portrait id，每行一条',
    creatorHelp: 'tieba.baidu.com/home 主页链接或 portrait id 均可',
    needsNode: false,
    media: 'none',
  },
  zhihu: {
    key: 'zhihu',
    label: PLATFORM_META.zhihu.label,
    modes: SEARCH_MODES,
    searchMinNotes: 20,
    detailPlaceholder: 'question / answer / zhuanlan / zvideo 链接（每行一条）',
    detailHelp: '问题、回答、专栏文章、视频链接都支持',
    creatorPlaceholder: 'https://www.zhihu.com/people/xxx，每行一条',
    creatorHelp: '原版 CLI 缺知乎创作者分支，需打补丁后才能用（环境自检会检测）',
    needsNode: true,
    media: 'none',
  },
}

/** 选择器里的平台顺序：按常用程度排 */
export const PLATFORM_ORDER: CrawlPlatform[] = ['xhs', 'dy', 'ks', 'bili', 'wb', 'tieba', 'zhihu']

/** 媒体下载范围的展示文案 */
export const MEDIA_SUPPORT_LABEL: Record<MediaSupport, string> = {
  'image+video': '图片 + 视频',
  image: '仅图片',
  video: '仅视频',
  none: '不下载',
}
