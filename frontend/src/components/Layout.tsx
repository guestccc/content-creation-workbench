import { Layout as AntLayout, Menu, Typography } from 'antd'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'

const { Text, Title } = Typography

/**
 * 侧边栏导航配置。
 *
 * key 就是路由路径 —— 直接把菜单项和路由绑在一起，省掉一层映射，
 * 也不会出现「菜单写了这个路径、路由表里却没有」的错位。
 */
const NAV_ITEMS = [
  { key: '/', label: '工作台概览', icon: '📊' },
  { key: '/contents', label: '内容管理', icon: '📝' },
  { key: '/scene', label: '智能镜头分割', icon: '✂️' },
  { key: '/mix', label: '智能混剪', icon: '🎬' },
]

/**
 * 全局布局组件：左侧固定导航栏 + 右侧内容区。
 *
 * 子路由通过 <Outlet /> 渲染。
 * 导航用 antd 的 Menu：选中态、键盘可达性、无障碍语义都由它负责，
 * 不用自己拼 NavLink 的 active 类名。
 */
export default function Layout() {
  const navigate = useNavigate()
  const { pathname } = useLocation()

  // 精确匹配 '/'，其余按前缀匹配（子路径也算选中这一项）
  const selectedKey =
    NAV_ITEMS.find((item) =>
      item.key === '/' ? pathname === '/' : pathname.startsWith(item.key),
    )?.key ?? '/'

  return (
    <AntLayout style={{ minHeight: '100vh' }}>
      <AntLayout.Sider
        theme="light"
        width={232}
        style={{ borderRight: '1px solid var(--color-border)' }}
      >
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            height: '100%',
            padding: '20px 8px 8px',
          }}
        >
          {/* 品牌区 */}
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '0 8px 20px' }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                width: 36,
                height: 36,
                fontSize: 16,
                fontWeight: 700,
                color: '#fff',
                background: 'linear-gradient(135deg, #3b82f6, #2563eb)',
                borderRadius: 10,
              }}
            >
              创
            </div>
            <div style={{ minWidth: 0 }}>
              <Title level={5} style={{ margin: 0, fontSize: 15 }}>
                内容创作工作台
              </Title>
              <Text type="secondary" style={{ fontSize: 11 }}>
                Content Workbench
              </Text>
            </div>
          </div>

          <Menu
            mode="inline"
            selectedKeys={[selectedKey]}
            items={NAV_ITEMS.map((item) => ({
              key: item.key,
              label: item.label,
              // emoji 当图标用：不额外引图标包，与其余页面的用法一致
              icon: <span style={{ fontSize: 15 }}>{item.icon}</span>,
            }))}
            onClick={({ key }) => navigate(key)}
            style={{ flex: 1, borderInlineEnd: 'none' }}
          />

          <div
            style={{
              padding: '12px 12px 0',
              borderTop: '1px solid var(--color-border)',
            }}
          >
            <Text type="secondary" style={{ fontSize: 11 }}>
              v0.1.0
            </Text>
          </div>
        </div>
      </AntLayout.Sider>

      <AntLayout.Content style={{ padding: '28px 32px', overflowX: 'auto' }}>
        <Outlet />
      </AntLayout.Content>
    </AntLayout>
  )
}
