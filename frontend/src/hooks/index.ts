/**
 * 自定义 hook 的统一出口。
 *
 * 页面里的逻辑按职责拆到这几个 hook 里，页面本身只负责「调 hook + 拼布局」：
 * - useApiMessage    接口失败提示（ApiError → 文案）
 * - useAsyncData     进页面拉一份数据（环境自检、模板、素材库）
 * - useSourceDir     素材目录扫描与勾选
 * - useDirectoryPicker  目录选择弹窗的开关
 * - useJobList       历史任务列表（分页 + 行多选）
 * - useJobRunner     当前任务：创建 / 轮询 / 取消 / 删除
 * - useJobPolling    单条任务的轮询（useJobRunner 内部用它，弹窗里单独盯一条任务时也用）
 * - usePurgeFiles    删除任务时「是否连同磁盘产物一起删」的勾选状态
 *
 * 新增页面时先来这里找有没有能复用的，别再往页面里写一套 useState + useEffect + fetch。
 */

export { useApiMessage } from './useApiMessage'
export type { UseApiMessageResult } from './useApiMessage'

export { useAsyncData } from './useAsyncData'
export type { UseAsyncDataOptions, UseAsyncDataResult } from './useAsyncData'

export { useDirectoryPicker } from './useDirectoryPicker'
export type { UseDirectoryPickerResult } from './useDirectoryPicker'

export { useJobList } from './useJobList'
export type { PagedList, UseJobListOptions, UseJobListResult } from './useJobList'

export { useJobPolling, POLL_INTERVAL_MS } from './useJobPolling'
export type { UseJobPollingOptions } from './useJobPolling'

export { useJobRunner } from './useJobRunner'
export type { UseJobRunnerOptions, UseJobRunnerResult } from './useJobRunner'

export { usePurgeFiles } from './usePurgeFiles'
export type { UsePurgeFilesResult } from './usePurgeFiles'

export { useSourceDir } from './useSourceDir'
export type { UseSourceDirResult } from './useSourceDir'
