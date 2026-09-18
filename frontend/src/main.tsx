import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'

import App from './App'
import './index.css'

const rootElement = document.getElementById('root')

if (!rootElement) {
  // 明确抛出而非静默失败，避免出现白屏却无从排查
  throw new Error('未找到挂载节点 #root，请检查 index.html')
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    {/*
      ConfigProvider 作用于全部页面：界面组件已统一为 antd，
      主题只在这一处定义，页面里不再各写各的颜色。

      刻意不引 antd 的全局 reset —— 组件样式是自带的（v6 起基于 CSS 变量），
      index.css 只提供 body 的字体/背景与设计变量，两下互不干扰。
      theme.token 与 index.css 里的 CSS 变量对齐，保证视觉是一套。
    */}
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: '#2563eb',
          colorSuccess: '#16a34a',
          colorWarning: '#d97706',
          colorError: '#dc2626',
          colorText: '#1f2328',
          colorTextSecondary: '#6b7280',
          colorBorder: '#e5e7eb',
          borderRadius: 10,
          borderRadiusSM: 6,
          borderRadiusLG: 14,
          fontFamily:
            "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', 'Segoe UI', Roboto, sans-serif",
          fontSize: 14,
        },
      }}
    >
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ConfigProvider>
  </React.StrictMode>,
)
