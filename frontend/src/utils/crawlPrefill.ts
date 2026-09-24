/**
 * 「素材抓取的一条笔记」→「换背景能直接用的一批原图」的翻译。
 *
 * 两个页面都要这一份转换：
 * - 素材抓取的结果弹窗里点某条笔记的「换背景」，跳过去之前先翻译好；
 * - 换背景页自己弹窗挑素材抓取产物，选中一条笔记时翻译。
 *
 * 两边逐字相同，所以要有一份公共实现（页面文件夹之间不许互相 import）。
 * 放 utils 而不是 types：`types/*.ts` 只放类型与状态常量，格式化的活归
 * `utils/format.ts` —— 这个既不是类型也不是格式化，单开一个文件。
 */

import type { BackgroundSwapPrefill } from '../types/background'
import { noteDisplayTitle } from '../types/crawler'
import type { CrawlNote } from '../types/crawler'

/**
 * 把抓取笔记的本地图转成换背景的带入载荷；没有本地图片时返回 null。
 *
 * 返回 null 与「换背景按钮置灰」用的是同一个判据（`local_image_dir` 为空），
 * 调用方拿到 null 就该当没这回事，不要硬造一个空目录的 prefill。
 *
 * 三处口径对照（改任一边都要同步这里）：
 * - `local_image_dir` —— 抓取产物 `<输出目录>/<平台>/images/<笔记 id>/` 的绝对路径，
 *   正好是换背景要的「一个原图目录」；
 * - `local_images` 是**相对**任务输出目录的路径，换背景只认纯文件名，所以取最后一段；
 * - `note.id` 是各平台原生的字符串 id，也是上面那个目录的名字 —— 存进
 *   `source_crawl_note_id` 后，抓取页才能按它把任务挂回笔记行。
 */
export function prefillFromCrawlNote(
  note: CrawlNote,
  crawlJobId: number,
): BackgroundSwapPrefill | null {
  if (!note.local_image_dir || note.local_images.length === 0) {
    return null
  }
  return {
    inputPath: note.local_image_dir,
    files: note.local_images.map((relative) => relative.split('/').pop() ?? relative),
    // 带上标题：连着给两条笔记换背景时，光看「任务 #12」分不出是哪条
    source: `素材抓取任务 #${crawlJobId} · ${noteDisplayTitle(note)}`,
    crawlJobId,
    crawlNoteId: note.id,
  }
}
