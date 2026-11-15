/**
 * E2E 专用 fixture（2026-11-15 S6.8 E16）。
 *
 * 只在 `import.meta.env.DEV` 为真时由 `App.tsx` 动态加载；生产构建不会挂载该路由。
 * 作用：在真实浏览器里制造一次**渲染期异常**，验证根级 ErrorBoundary 的可见 fallback，
 * 而不是只靠组件单测。
 */
export default function RenderErrorFixture(): never {
  throw new Error('E2E_RENDER_ERROR_FIXTURE')
}
