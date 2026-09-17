import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite 配置文档：https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 开发环境把 /api 请求代理到后端服务，
    // 这样前端统一使用相对路径请求，既避免跨域，也无需维护两套地址。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
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
        },
      },
    },
  },
})
