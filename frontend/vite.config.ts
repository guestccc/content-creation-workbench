import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite 配置文档：https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 开发环境把 /api 请求代理到后端服务，
    // 这样前端统一使用相对路径请求，既避免跨域，也无需维护两套地址。
    // 目标地址可用 VITE_PROXY_TARGET 覆盖（比如后端换到别的端口启动时，
    // 不用改配置文件：VITE_PROXY_TARGET=http://127.0.0.1:8001 npm run dev）。
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    // 生产构建输出目录
    outDir: 'dist',
    // 打包体积较大的依赖单独分包，利于浏览器缓存
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          // antd 与它的图标包体量大（全站组件都来自它），单独切一块供应商包：
          // 它不随业务代码改动而失效，浏览器可以长期缓存
          antd: ['antd', '@ant-design/icons'],
          // @ant-design/x 只有 AI 文案弹窗用得上，跟 antd 分开放：
          // 混在一起的话，以后升 x 会让 antd 那块缓存也跟着失效
          'ant-design-x': ['@ant-design/x'],
        },
      },
    },
  },
})
