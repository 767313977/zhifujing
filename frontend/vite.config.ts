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
  build: {
    // 产物文件名统一加一个固定后缀 `-r2`（2026-10-12 加）。
    //
    // 为什么需要：白屏事故期间，「不存在的文件」也被打上了 `immutable, max-age=1 年`
    // （根因已在 `backend/app/main.py` 的 `cache_headers` 修掉），于是那段时间打开过
    // 网站的浏览器把「Login-xxx.js 不存在」缓存了一年。而恢复出来的文件名与事故前
    // **完全一样**（内容没变、哈希就不变），这些浏览器会继续用缓存里的 404、照样白屏，
    // 只有硬刷新/清缓存能救。给文件名加个后缀就让整批产物换名，缓存自然绕开。
    //
    // 这段留着不影响什么：以后要再来一次同样的「强制换名」，把 r2 改成 r3 即可。
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name]-[hash]-r2.js',
        chunkFileNames: 'assets/[name]-[hash]-r2.js',
      },
    },
  },
})
