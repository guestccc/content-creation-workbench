import {
  CloudDownloadOutlined,
  DashboardOutlined,
  FileTextOutlined,
  FontSizeOutlined,
  RocketOutlined,
  ScissorOutlined,
  TeamOutlined,
  VideoCameraOutlined,
} from '@ant-design/icons'
import { Layout as AntLayout, Menu, Typography } from 'antd'
import type { ReactNode } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'

const { Text, Title } = Typography

/** 导航叶子项（可直接点进去的页面） */
interface NavLeaf {
  key: string
  label: string
  icon: ReactNode
}

/** 分组导航项：渲染成 antd Menu 的 group（只是分组标题，不可选中） */
interface NavGroup {
  type: 'group'
  key: string
  label: string
  children: NavLeaf[]
}

/**
 * 侧边栏导航配置。
 *
 * 叶子项的 key 就是路由路径 —— 直接把菜单项和路由绑在一起，省掉一层映射，
 * 也不会出现「菜单写了这个路径、路由表里却没有」的错位。
 */
const NAV_ITEMS: (NavLeaf | NavGroup)[] = [
  { key: '/', label: '工作台概览', icon: <DashboardOutlined /> },
  { key: '/contents', label: '内容管理', icon: <FileTextOutlined /> },
  {
    type: 'group',
    key: 'g-video',
    label: '视频二创',
    children: [
      // 切分用剪刀（把镜头剪开），混剪用摄像机（把片段拼成片），一键成品用火箭（直接出片），字幕用文字
      { key: '/scene', label: '智能镜头分割', icon: <ScissorOutlined /> },
      { key: '/mix', label: '智能混剪', icon: <VideoCameraOutlined /> },
      { key: '/finalcut', label: '一键成品', icon: <RocketOutlined /> },
      { key: '/subtitle', label: '字幕提取', icon: <FontSizeOutlined /> },
    ],
  },
  {
    type: 'group',
    key: 'g-rewrite',
    label: '图文二创',
    children: [
      { key: '/crawl', label: '素材抓取', icon: <CloudDownloadOutlined /> },
      { key: '/creators', label: '创作者主页', icon: <TeamOutlined /> },
    ],
  },
]

/** 摊平后的全部叶子：选中态匹配只看叶子（分组标题不是路由） */
const NAV_LEAVES: NavLeaf[] = NAV_ITEMS.flatMap((item) =>
  'children' in item ? item.children : [item],
)

/** Menu 的 items：叶子原样，分组转成 antd 的 group 结构 */
const MENU_ITEMS = NAV_ITEMS.map((item) =>
  'children' in item
    ? {
        type: 'group' as const,
        key: item.key,
        label: item.label,
        children: item.children.map((leaf) => ({
          key: leaf.key,
          label: leaf.label,
          icon: leaf.icon,
        })),
      }
    : { key: item.key, label: item.label, icon: item.icon },
)

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

  // 精确匹配 '/'，其余按前缀匹配（子路径也算选中这一项）；在摊平的叶子里找
  const selectedKey =
    NAV_LEAVES.find((item) =>
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
            items={MENU_ITEMS}
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
