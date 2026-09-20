/**
 * 本地文件系统浏览与目录收藏接口。
 *
 * 浏览器拿不到本地绝对路径（<input type="file"> 只给文件名），
 * 所以「选输入/输出目录」必须由后端列目录。
 */

import { BASE_URL, del, get, post } from './client'
import type { FsFavorite, FsListData } from '../types/scene'

/** 列出指定目录的内容；不传 path 时列出用户主目录 */
export function fetchDirectory(path?: string): Promise<FsListData> {
  return get<FsListData>('/fs/list', path ? { path } : undefined)
}

/**
 * 任意本地视频的只读预览流地址（直接塞给 `<video src>`，支持 Range）。
 *
 * 素材列表「看一眼再勾选」、一键成品的框选步骤都用它。
 */
export function localVideoPreviewUrl(path: string): string {
  return `${BASE_URL}/fs/preview?path=${encodeURIComponent(path)}`
}

/** 收藏的目录清单（目录选择弹窗左侧那一列） */
export function fetchFavorites(): Promise<FsFavorite[]> {
  return get<FsFavorite[]>('/fs/favorites')
}

/** 收藏一个目录（重复收藏幂等，返回已有条目） */
export function addFavorite(path: string): Promise<FsFavorite> {
  return post<FsFavorite>('/fs/favorites', { path })
}

/** 取消收藏（磁盘上的目录与文件不受影响） */
export function removeFavorite(favoriteId: string): Promise<{ id: string }> {
  return del<{ id: string }>(`/fs/favorites/${favoriteId}`)
}
