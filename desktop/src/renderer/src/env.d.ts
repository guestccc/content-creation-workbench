/// <reference types="vite/client" />

import type { DesktopApi } from '@shared/types'

declare global {
  interface Window {
    /** 由预加载脚本注入的客户端接口 */
    api: DesktopApi
  }
}

export {}
