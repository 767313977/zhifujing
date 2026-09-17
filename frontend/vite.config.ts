import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 开发期把 /api 代理到 FastAPI，避免跨域
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
