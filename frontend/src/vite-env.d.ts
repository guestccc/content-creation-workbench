/// <reference types="vite/client" />

/** 项目自定义环境变量类型声明 */
interface ImportMetaEnv {
  /** 后端接口基础路径，例如 /api/v1 */
  readonly VITE_API_BASE_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
