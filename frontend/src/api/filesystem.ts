/**
 * 本地文件系统浏览接口。
 *
 * 浏览器拿不到本地绝对路径（<input type="file"> 只给文件名），
 * 所以「选输入/输出目录」必须由后端列目录。
 */

import { get } from './client'
import type { FsListData } from '../types/scene'

/** 列出指定目录的内容；不传 path 时列出用户主目录 */
export function fetchDirectory(path?: string): Promise<FsListData> {
  return get<FsListData>('/fs/list', path ? { path } : undefined)
}
