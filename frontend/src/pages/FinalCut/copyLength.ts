/**
 * 口播稿的字数与秒数换算（本页私有：只有一键成品页用，不进 src/hooks）。
 *
 * 三个函数都是后端 `services/finalcut_copy.py` 那套算法的镜像，**常量改动要
 * 两边一起改**：
 * - `countCopyChars` ↔ `count_chars`（含标点、不含空白）
 * - `copyCharBudget` ↔ `char_budget`（时长 × 语速，±10%）
 *
 * 权威值仍在后端：预算写进提示词、字数由解析侧实测落库。页面这一份只负责
 * 「删掉两句立刻看到秒数怎么变」的即时反馈 —— 正文是可编辑的，等后端返回
 * 就太晚了。
 *
 * 已知的一处不齐：JS 的 `Math.round` 与 Python 的银行家舍入在 .5 边界上会
 * 差 1 字（如 82.5 → 83 / 82）。预算的权威值由后端算，页面差这 1 字不影响
 * 「念出来够不够长」的判断，不做对齐。
 */

/** 预算容差（±10%）与保底，与后端 _BUDGET_TOLERANCE / _MIN_BUDGET_* 一致 */
const BUDGET_TOLERANCE = 0.1
const MIN_BUDGET_LOW = 10
const MIN_BUDGET_SPAN = 5

/**
 * 口播字数：含标点、不含换行与空白。
 *
 * 用 `Array.from` 而不是 `.length`：emoji、部分生僻字在 UTF-16 里占两个码元，
 * `.length` 会比后端口径多算（`len()` 按码点数）。
 */
export function countCopyChars(text: string): number {
  return Array.from(text).filter((char) => !/\s/.test(char)).length
}

/** 这段文案按给定语速念出来大约几秒；语速还没探测到（0）时返回 0 */
export function estimateSeconds(text: string, charsPerSecond: number): number {
  if (!charsPerSecond) {
    return 0
  }
  return countCopyChars(text) / charsPerSecond
}

/**
 * 字数预算 [下限, 上限]；语速还没探测到（0）时返回 null（页面只显示字数）。
 *
 * 时长取任务记录里的 video_duration（后端 ffprobe 实测），不是页面上显示的
 * 那个 —— 本地文件的 SelectedMaterial.duration_seconds 是 null。
 */
export function copyCharBudget(
  durationSeconds: number,
  charsPerSecond: number,
): [number, number] | null {
  if (!charsPerSecond) {
    return null
  }
  const target = Math.max(0, durationSeconds) * charsPerSecond
  const low = Math.max(MIN_BUDGET_LOW, Math.round(target * (1 - BUDGET_TOLERANCE)))
  const high = Math.max(low + MIN_BUDGET_SPAN, Math.round(target * (1 + BUDGET_TOLERANCE)))
  return [low, high]
}
