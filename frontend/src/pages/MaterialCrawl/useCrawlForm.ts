/**
 * 素材抓取表单的状态机（本页私有，不进公共 hooks）。
 *
 * 只做四件事：
 * 1. 平台 ↔ 模式联动：切平台时，当前模式不被支持就退回第一个支持的模式，
 *    数量抬到平台每页最小值（低于它平台会强制抬升，预估会失真）；
 * 2. 持有三种模式各自的输入（关键词/链接/创作者）与登录方式输入；
 * 3. 「从创作者库选人」：选中项按行去重并入创作者输入框（选择框只是插入器，
 *    反选不会把已填的行删掉 —— 输入框才是最终事实源）；
 * 4. buildPayload()：校验 + 组装成创建任务的请求体，校验不过给一句人话文案，
 *    页面只负责 message.warning 弹出来。
 */

import { useCallback, useState } from 'react'

import type {
  CrawlJobPayload,
  CrawlLoginType,
  CrawlPlatform,
  CrawlerType,
} from '../../types/crawler'
import { PLATFORM_SPECS } from '../../types/crawler'

/** buildPayload 的返回：要么给请求体，要么给一句提示文案（带 ok 标记方便收窄） */
export type CrawlFormBuild =
  | { ok: true; payload: CrawlJobPayload }
  | { ok: false; error: string }

/**
 * 解析多行输入：trim、去空行、去重保序（与后端清洗规则一致）。
 * 关键词额外支持逗号分隔（用户习惯「保温杯，焖烧杯」一行的写法）。
 */
function parseLines(text: string, commaAware: boolean): string[] {
  const parts = commaAware ? text.split(/[\r\n,，]+/) : text.split(/\r?\n/)
  const seen = new Set<string>()
  const result: string[] = []
  for (const part of parts) {
    const cleaned = part.trim()
    if (!cleaned || seen.has(cleaned)) {
      continue
    }
    seen.add(cleaned)
    result.push(cleaned)
  }
  return result
}

const DEFAULT_PLATFORM: CrawlPlatform = 'xhs'
const DEFAULT_TYPE: CrawlerType = 'search'

export function useCrawlForm() {
  const [platform, setPlatformState] = useState<CrawlPlatform>(DEFAULT_PLATFORM)
  const [crawlerType, setCrawlerTypeState] = useState<CrawlerType>(DEFAULT_TYPE)
  const [loginType, setLoginType] = useState<CrawlLoginType>('qrcode')
  const [keywordsText, setKeywordsText] = useState('')
  const [idsText, setIdsText] = useState('')
  const [creatorsText, setCreatorsText] = useState('')
  const [librarySelection, setLibrarySelectionState] = useState<string[]>([])
  const [cookies, setCookies] = useState('')
  const [startPage, setStartPage] = useState(1)
  const [maxNotes, setMaxNotes] = useState(20)
  const [getComments, setGetComments] = useState(true)
  const [getSubComments, setGetSubComments] = useState(false)
  const [maxComments, setMaxComments] = useState(10)
  const [headless, setHeadless] = useState(false)
  const [maxConcurrency, setMaxConcurrency] = useState(1)

  /** 切平台：修正不支持的模式、把数量抬到每页最小值、清掉库选人（主页形态不通用） */
  const setPlatform = useCallback(
    (next: CrawlPlatform) => {
      setPlatformState(next)
      setLibrarySelectionState([])
      const nextSpec = PLATFORM_SPECS[next]
      if (!nextSpec.modes.includes(crawlerType)) {
        setCrawlerTypeState(nextSpec.modes[0])
      }
      if (nextSpec.searchMinNotes > 0) {
        setMaxNotes((current) => Math.max(current, nextSpec.searchMinNotes))
      }
    },
    [crawlerType],
  )

  /** 从创作者库选人：选中项按行去重并入 creatorsText，选择框自身只做插入器 */
  const setLibrarySelection = useCallback((values: string[]) => {
    setLibrarySelectionState(values)
    if (values.length === 0) {
      return
    }
    setCreatorsText((current) => {
      const existing = new Set(
        current
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter(Boolean),
      )
      const added = values.filter((value) => !existing.has(value.trim()))
      if (added.length === 0) {
        return current
      }
      const kept = current.split(/\r?\n/).filter((line) => line.trim())
      return [...kept, ...added].join('\n')
    })
  }, [])

  /** 切模式：不在当前平台支持列表内的直接忽略（Segmented 的可选项已经限制了，这是兜底） */
  const setCrawlerType = useCallback(
    (next: CrawlerType) => {
      if (PLATFORM_SPECS[platform].modes.includes(next)) {
        setCrawlerTypeState(next)
      }
    },
    [platform],
  )

  /** 校验并组装请求体；校验不过返回提示文案 */
  const buildPayload = useCallback((): CrawlFormBuild => {
    const keywords = parseLines(keywordsText, true)
    const ids = parseLines(idsText, false)
    const creators = parseLines(creatorsText, false)

    if (crawlerType === 'search' && keywords.length === 0) {
      return { ok: false, error: '请至少填写一个关键词（每行一个，也可用逗号分隔）' }
    }
    if (crawlerType === 'detail' && ids.length === 0) {
      return { ok: false, error: '请至少填写一条笔记 / 作品链接' }
    }
    if (crawlerType === 'creator' && creators.length === 0) {
      return { ok: false, error: '请至少填写一位创作者主页链接或 ID' }
    }
    if (loginType === 'cookie' && !cookies.trim()) {
      return { ok: false, error: 'Cookie 登录需要先粘贴 Cookie 串' }
    }

    const payload: CrawlJobPayload = {
      platform,
      crawler_type: crawlerType,
      login_type: loginType,
      max_notes: Math.max(1, maxNotes),
      get_comments: getComments,
      get_sub_comments: getSubComments,
      max_comments: Math.max(0, maxComments),
      max_concurrency: Math.min(3, Math.max(1, maxConcurrency)),
      // 扫码登录必须有浏览器界面，headless 强制关（后端同样兜底）
      headless: loginType === 'qrcode' ? false : headless,
    }
    if (crawlerType === 'search') {
      payload.keywords = keywords
      payload.start_page = Math.max(1, startPage)
    } else if (crawlerType === 'detail') {
      payload.ids = ids
    } else {
      payload.creators = creators
    }
    if (loginType === 'cookie') {
      payload.cookies = cookies.trim()
    }
    return { ok: true, payload }
  }, [
    platform,
    crawlerType,
    loginType,
    keywordsText,
    idsText,
    creatorsText,
    cookies,
    startPage,
    maxNotes,
    getComments,
    getSubComments,
    maxComments,
    headless,
    maxConcurrency,
  ])

  /** 一键还原默认值 */
  const reset = useCallback(() => {
    setPlatformState(DEFAULT_PLATFORM)
    setCrawlerTypeState(DEFAULT_TYPE)
    setLoginType('qrcode')
    setKeywordsText('')
    setIdsText('')
    setCreatorsText('')
    setLibrarySelectionState([])
    setCookies('')
    setStartPage(1)
    setMaxNotes(20)
    setGetComments(true)
    setGetSubComments(false)
    setMaxComments(10)
    setHeadless(false)
    setMaxConcurrency(1)
  }, [])

  return {
    platform,
    setPlatform,
    crawlerType,
    setCrawlerType,
    loginType,
    setLoginType,
    keywordsText,
    setKeywordsText,
    idsText,
    setIdsText,
    creatorsText,
    setCreatorsText,
    librarySelection,
    setLibrarySelection,
    cookies,
    setCookies,
    startPage,
    setStartPage,
    maxNotes,
    setMaxNotes,
    getComments,
    setGetComments,
    getSubComments,
    setGetSubComments,
    maxComments,
    setMaxComments,
    headless,
    setHeadless,
    maxConcurrency,
    setMaxConcurrency,
    buildPayload,
    reset,
  }
}
