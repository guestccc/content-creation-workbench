import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import App from './App'
import './index.css'

const rootElement = document.getElementById('root')

if (!rootElement) {
  // 明确抛出而非静默失败，避免出现白屏却无从排查
  throw new Error('未找到挂载节点 #root，请检查 index.html')
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
