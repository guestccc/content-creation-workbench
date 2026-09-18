import { Navigate, Route, Routes } from 'react-router-dom'

import Layout from './components/Layout'
import ContentList from './pages/ContentList'
import Dashboard from './pages/Dashboard'
import MixCut from './pages/MixCut'
import SceneSplit from './pages/SceneSplit'

/**
 * 应用根组件。
 *
 * 所有页面共享 Layout（左侧导航 + 右侧内容区），
 * 未匹配的路径统一重定向到工作台概览。
 */
export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/contents" element={<ContentList />} />
        {/* 必须在通配路由之前，否则会被重定向到首页 */}
        <Route path="/scene" element={<SceneSplit />} />
        <Route path="/mix" element={<MixCut />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
