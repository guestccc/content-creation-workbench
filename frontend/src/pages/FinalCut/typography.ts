/**
 * 框内实时预览用的排版估算 —— 后端 finalcut_render.py 的 TS 移植。
 *
 * 只决定「字号与换行点」：按估算字宽（CJK≈1.0em、ASCII≈0.55em）贪心折行，
 * 字号从大到小逐档试，第一个竖向放得下的胜出。两边算法一致，框里看到的
 * 行数/字号与最终烧进画面的基本一致（居中由 ffmpeg 的 text_w/text_h 实测
 * 兜底，估算误差只表现为轻微溢出，不会偏右）。
 *
 * JS 没有 unicodedata.east_asian_width，用码点区间近似（W/F 全宽与常见
 * ambiguous 标点都按 1.0em 算）—— 预览估算，不要求逐字精确。
 */

/** 估算为全宽的码点区间（CJK 各区块 + 全角字符 + 常见 ambiguous 标点） */
const WIDE_CHAR =
  /[·—‘’“”…‰′″℃℉ᄀ-ᅟ⺀-⿟㐀-䶿一-鿿가-힣豈-﫿︰-﹏＀-｠￠-￦]/

/** 单个字符的估算宽度（px），与后端 estimate_char_width 对应 */
export function estimateCharWidth(ch: string, fontSize: number): number {
  return fontSize * (WIDE_CHAR.test(ch) ? 1.0 : 0.55)
}

/** 按框宽贪心折行（尊重原文 \n，空行丢弃），与后端 wrap_text 对应 */
export function wrapText(text: string, boxW: number, fontSize: number): string[] {
  const lines: string[] = []
  for (const raw of (text || '').split('\n')) {
    const trimmed = raw.trim()
    if (!trimmed) {
      continue
    }
    let current = ''
    let width = 0
    for (const ch of trimmed) {
      const charW = estimateCharWidth(ch, fontSize)
      if (current && width + charW > boxW) {
        lines.push(current)
        current = ch
        width = charW
      } else {
        current += ch
        width += charW
      }
    }
    if (current) {
      lines.push(current)
    }
  }
  return lines
}

/** 自动字号的尝试范围与竖向松弛系数（与后端常量一致） */
export const AUTO_MIN_FONT_SIZE = 16
export const AUTO_MAX_FONT_SIZE = 64
const HEIGHT_SLACK = 1.15

export interface FitResult {
  fontSize: number
  lines: string[]
  /** 降到最小字号仍放不下时发生了截断（后端会把原因写进 item error） */
  truncated: boolean
}

/** 自动字号：从大到小逐档试，取第一个竖向放得下的字号（与后端 fit_font_size 对应） */
export function fitFontSize(
  text: string,
  boxW: number,
  boxH: number,
  lineSpacing = 8,
): FitResult {
  for (let size = AUTO_MAX_FONT_SIZE; size >= AUTO_MIN_FONT_SIZE; size -= 4) {
    const lines = wrapText(text, boxW, size)
    if (lines.length === 0) {
      break
    }
    const totalH = lines.length * size + lineSpacing * (lines.length - 1)
    if (totalH <= boxH * HEIGHT_SLACK) {
      return { fontSize: size, lines, truncated: false }
    }
  }
  const lines = wrapText(text, boxW, AUTO_MIN_FONT_SIZE)
  if (lines.length === 0) {
    return { fontSize: AUTO_MIN_FONT_SIZE, lines: [''], truncated: false }
  }
  const maxLines = Math.max(
    1,
    Math.floor((boxH * HEIGHT_SLACK + lineSpacing) / (AUTO_MIN_FONT_SIZE + lineSpacing)),
  )
  if (lines.length > maxLines) {
    return { fontSize: AUTO_MIN_FONT_SIZE, lines: lines.slice(0, maxLines), truncated: true }
  }
  return { fontSize: AUTO_MIN_FONT_SIZE, lines, truncated: false }
}
