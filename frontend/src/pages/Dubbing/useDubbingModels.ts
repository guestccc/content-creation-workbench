/**
 * 配音可选模型（含每个的下载状态）。
 *
 * 为什么单独一个 hook 且**不**进公共 hooks：只有配音页用得到，而且它跟着另一套
 * 外部依赖走（哪台机器上装了哪一版 Voicebox）。判定标准就一条 —— 有没有第二个
 * 页面要用 —— 没有，所以留在本页文件夹里。
 *
 * 候选**不写死在前端**：上游 /models/status 才是「有哪些引擎、哪个下好了」的
 * 真相，而后端还替我们做了两件事（见 services/voicebox_models.py）：把混在同一
 * 份清单里的 whisper 转写、qwen3 大模型筛掉；把上游扁平的 model_name 翻成
 * /generate 真正要吃的那对 (engine, model_size)。
 *
 * 拉不到时**不弹提示**：服务没开这件事由页面上那条环境 Alert 负责说，这里再弹
 * 一次就是同一件事说两遍 —— 与 useDubbing 拉音色的口径一致（Select 的空态会
 * 自己指向该做什么）。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { fetchDubbingModels } from '../../api/voicebox'
import type { DubbingModel } from '../../types/voicebox'
import { modelKey } from '../../types/voicebox'

export interface UseDubbingModelsResult {
  /** 可选的配音模型（拉不到时为空数组） */
  models: DubbingModel[]
  loading: boolean
  /** 重新拉一次（「重新检测」时顺带拉，刚在 Voicebox 里下好模型后点它） */
  reload: () => Promise<void>
  /** 用户没选过时该默认选中哪个（模型键）；列表为空时是空串 */
  defaultKey: string
  /** 按模型键取一项；键对不上（比如刚刷新后这个模型没了）返回 undefined */
  find: (key: string) => DubbingModel | undefined
}

export function useDubbingModels(): UseDubbingModelsResult {
  const [models, setModels] = useState<DubbingModel[]>([])
  const [loading, setLoading] = useState(false)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const data = await fetchDubbingModels()
      setModels(data.items)
    } catch {
      setModels([])
    } finally {
      setLoading(false)
    }
  }, [])

  // 进页面拉一次。服务没开时这次会失败（静默清空），用户点「重新检测」会再来一次。
  useEffect(() => {
    void reload()
  }, [reload])

  const find = useCallback(
    (key: string): DubbingModel | undefined => models.find((item) => modelKey(item) === key),
    [models],
  )

  /**
   * 默认选哪个：**已加载进显存的那个**（那是 Voicebox 现在真在用、也是立刻能跑的），
   * 没有就退而选一个下好的，再没有就退回列表第一项（后端目录里排第一的是质量优先的
   * Qwen 1.7B）。
   *
   * 为什么不是固定选第一个：第一个很可能**没下载**，用户点「生成配音」会先等一个
   * 几 GB 的下载，而他自己并不知道 —— 机器上已经有一个下好、加载好的，没有理由不用。
   */
  const defaultKey = useMemo(() => {
    if (models.length === 0) return ''
    const loaded = models.find((item) => item.loaded)
    const downloaded = models.find((item) => item.downloaded === true)
    return modelKey(loaded ?? downloaded ?? models[0])
  }, [models])

  return { models, loading, reload, defaultKey, find }
}
