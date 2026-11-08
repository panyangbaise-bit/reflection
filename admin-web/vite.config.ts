import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 静态托管（Caddy try_files → index.html）+ hash 路由，相对 base 便于部署到任意路径。
export default defineConfig({
  plugins: [vue()],
  base: './',
  server: {
    port: 5173,
    proxy: {
      // 本地联调：api 服务在 compose 中映射到 127.0.0.1:18000（部署后由 Caddy 反代 /api/admin/v1/*）。
      '/api': {
        target: 'http://127.0.0.1:18000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    target: 'es2020',
    rollupOptions: {
      output: {
        manualChunks: {
          'vue-vendor': ['vue', 'vue-router'],
          'naive-ui': ['naive-ui'],
          axios: ['axios'],
        },
      },
    },
  },
})
