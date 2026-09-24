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

/** running 任务的当前阶段（与后端 detect_phase 返回值对应） */
export type CrawlPhase =
  | 'starting'
  | 'login_cookie'
  | 'login_scan'
  | 'login_redirect'
  | 'crawling'
  | 'finishing'

/** 阶段展示配置：label 用于 Steps 步骤名，hint 告诉用户当前该做什么 */
export const PHASE_META: Record<CrawlPhase, { label: string; hint: string }> = {
  starting: { label: '启动浏览器', hint: '正在初始化浏览器环境' },
  login_cookie: { label: 'Cookie 登录', hint: '正在用 Cookie 验证登录态' },
  login_scan: {
    label: '等待扫码',
    hint: '二维码已在系统看图软件弹出，用手机扫码后在 120 秒内确认',
  },
  login_redirect: { label: '登录成功', hint: '登录成功，等待页面跳转' },
  crawling: { label: '抓取数据', hint: '正在抓取笔记数据' },
  finishing: { label: '收尾', hint: '正在关闭浏览器并保存数据' },
}

/** 按登录方式返回阶段序列（Steps 展示用，cookie 没有扫码/跳转环节） */
export function phaseSteps(loginType: CrawlLoginType): CrawlPhase[] {
  if (loginType === 'cookie') {
    return ['starting', 'login_cookie', 'crawling', 'finishing']
  }
  return ['starting', 'login_scan', 'login_redirect', 'crawling', 'finishing']
}

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
  /** 备注（用户可编辑，最多 200 字；空串表示没写）。
   *  注意别和上面的 note_count 混了：那是「抓到的笔记数」，这个才是备注 */
  remark: string
  /** running 任务的当前阶段（starting、login_前缀、crawling、finishing），非 running 为空串 */
  phase: string
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
  /** MC 落盘为字符串（如 "6.6万"），原样透传；排序用 parseCountValue 转数值 */
  liked_count: string
  collected_count: string
  comment_count: string
  share_count: string
  publish_time: string
  url: string
  cover: string
  images: string[]
  /** 相对任务输出目录的本地图片路径（走 /media 接口取） */
  local_images: string[]
  local_videos: string[]
  /**
   * 本地图片所在目录的绝对路径；没有本地图片时为空串。
   *
   * 给「一键换背景」用：那边要的是「一个原图目录 + 一批文件名」，
   * 而 local_images 是相对路径，换不过去。
   */
  local_image_dir: string
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

/** 一个平台的候选文案（标题 + 简介） */
export interface NotePlatformCopy {
  /** 候选标题（至多 5 条；模型少给就少给，按实际渲染） */
  titles: string[]
  /** 候选简介（至多 3 条） */
  intros: string[]
}

/** 一条笔记的 AI 文案生成结果（小红书 + 抖音各一份） */
export interface CrawlNoteAiCopy {
  job_id: number
  note_id: string
  platforms: {
    xhs: NotePlatformCopy
    dy: NotePlatformCopy
  }
  /** 生成用的模型名 */
  model: string
  /** 本次生成的 token 用量 */
  tokens_used: number
  /** 模型思维链原文；老行或模型不支持思考时是空串（此时弹窗整块不渲染） */
  reasoning: string
  updated_at: string
}

/** AI 文案读取接口返回；found=false 表示还没生成过（正常态，不是错误） */
export interface CrawlNoteAiCopyData {
  found: boolean
  result: CrawlNoteAiCopy | null
}

// ---------------------------------------------------------------------------
// 评论（查看 / 补抓）
// ---------------------------------------------------------------------------

/** 一条归一化后的评论（跨平台字段已对齐，见 services/crawl_comments.py） */
export interface CrawlComment {
  id: string
  content: string
  nickname: string
  /**
   * 点赞数，**字符串**原样透传。
   *
   * 空串表示「这个平台不落盘点赞」（快手 / 贴吧），要能与 "0"（真的 0 赞）
   * 区分开 —— 都渲染成 0 是在编数据。
   */
  liked_count: string
  /** 归一化后的时间；认不出时是空串（不是无效值，只是没得显示） */
  created_at: string
  /** 平台说这条评论有多少条子评论（含没抓到的，见 CommentsModal 里的差额提示） */
  sub_comment_count: number
  /**
   * 评论图地址，**两种形态混合**：
   * - `http(s)…` 平台原图 URL —— 带时效签名，过期即 403（通常是后端没缓存
   *   成功的），裂图只能靠「再抓一次」刷新签名；
   * - 其余是本地缓存路径（后端读评论时懒下载的），用 crawlMediaUrl(jobId, path) 取。
   */
  pictures: string[]
  /** 父评论没被抓到（截断 / 风控），提升成一级展示但必须标明 */
  orphan: boolean
  children: CrawlComment[]
}

/** 原任务的评论采集配置（弹窗三态判定的依据） */
export interface CrawlCommentsConfig {
  /** get_comments 且 max_comments > 0 才算真的会抓 */
  enabled: boolean
  max_comments: number
  sub_comments: boolean
}

/** 最新一次评论补抓任务的状态（补抓任务不进历史列表，这是它唯一的可见入口） */
export interface CrawlCommentRefetchState {
  /** 补抓任务 id（取消走 cancelCrawlJob） */
  job_id: number
  status: CrawlJobStatus
  error_message: string
  /** 这次补抓自己抓到的评论条数（不是任务的 note_count，那边数的是内容行） */
  comment_count: number
  /** 排在它前面的待执行任务数（MC 单任务串行，用来解释「为什么还在转圈」） */
  queued_ahead: number
}

/** 一条笔记的评论查询结果 */
export interface NoteCommentsData {
  /** 根任务 id（拿派生任务 id 查也会解析回根任务） */
  job_id: number
  note_id: string
  /** 平台标识（补抓设置里的 Cookie 库按它过滤） */
  platform: CrawlPlatform
  comments: CrawlComment[]
  /** 评论总条数（含所有层级） */
  total: number
  /** 一级评论条数 */
  top_level_total: number
  comments_config: CrawlCommentsConfig
  /** 根任务的登录方式（补抓设置里登录方式单选的默认选中项） */
  login_type: CrawlLoginType
  /** 根任务是否无头跑浏览器（补抓设置里无头开关的默认值） */
  headless: boolean
  refetch: CrawlCommentRefetchState | null
}

/** 补抓一条笔记评论的请求体 */
export interface CrawlCommentRefetchPayload {
  note_id: string
  /** 一级评论条数上限（后端限 1~200） */
  max_comments: number
  sub_comments: boolean
  /** 不传 = 沿用原任务的登录方式 */
  login_type?: CrawlLoginType
  /** 新贴的 Cookie 串；cookie 登录且不传（留空）时服务端沿用原任务存的 */
  cookies?: string
  /** 不传 = 沿用原任务的无头设置 */
  headless?: boolean
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

/**
 * 互动数转数值（结果列表排序用）："6.6万" → 66000、"1.2亿" → 1.2e8、
 * "3.5w"/"2k" 与纯数字也认；空值和认不出的给 0，排序时沉底。
 */
export function parseCountValue(text: string): number {
  const match = text.trim().match(/^([\d.,]+)\s*(万|亿|w|k|千)?$/i)
  if (!match) {
    return 0
  }
  const number = Number.parseFloat(match[1].replace(/,/g, ''))
  if (!Number.isFinite(number)) {
    return 0
  }
  const unit = match[2]?.toLowerCase()
  if (unit === '万' || unit === 'w') {
    return number * 1e4
  }
  if (unit === '亿') {
    return number * 1e8
  }
  if (unit === 'k' || unit === '千') {
    return number * 1e3
  }
  return number
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
