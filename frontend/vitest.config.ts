import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

/**
 * 前端状态机单测（2026-11-08 第四轮复审）。
 *
 * 只跑 src 目录下的单元测试；环境用 jsdom 提供 localStorage / StorageEvent。
 * 与 `vite.config.ts` 分开，避免把 tailwind 插件带进测试进程。
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    restoreMocks: true,
    clearMocks: true,
  },
})
