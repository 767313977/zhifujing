import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 开发期把 /api 代理到 FastAPI，避免跨域。
    //
    // 端口走环境变量：本机（Windows）后端在 8000，同伴的 Mac 上在 8002
    // （他那边 8000 被别的程序占着）。写死哪一个都会让另一边每次 pull 都改这行，
    // 所以默认 8000、要换就在起 dev 时覆盖：
    //   VITE_API_PROXY=http://127.0.0.1:8002 npm run dev
    proxy: {
      '/api': process.env.VITE_API_PROXY || 'http://127.0.0.1:8000',
    },
  },
})
