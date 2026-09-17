import { NavLink, Outlet } from 'react-router-dom'

/** 侧边栏导航配置 */
const NAV_ITEMS = [
  { to: '/', label: '工作台概览', icon: '📊', end: true },
  { to: '/contents', label: '内容管理', icon: '📝', end: false },
]

/**
 * 全局布局组件：左侧固定导航栏 + 右侧内容区。
 *
 * 子路由通过 <Outlet /> 渲染。
 */
export default function Layout() {
  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="sidebar__brand">
          <span className="sidebar__logo">创</span>
          <div className="sidebar__brand-text">
            <h1 className="sidebar__title">内容创作工作台</h1>
            <p className="sidebar__subtitle">Content Workbench</p>
          </div>
        </div>

        <nav className="sidebar__nav">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => `nav-item${isActive ? ' nav-item--active' : ''}`}
            >
              <span className="nav-item__icon" aria-hidden="true">
                {item.icon}
              </span>
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>

        <footer className="sidebar__footer">v0.1.0</footer>
      </aside>

      <main className="content">
        <Outlet />
      </main>
    </div>
  )
}
