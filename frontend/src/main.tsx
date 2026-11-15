import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import ErrorBoundary from './components/ErrorBoundary.tsx'
import { decideChunkReload } from './lib/chunkReload'

/**
 * 代码块（chunk）加载失败的一次性自愈（2026-10-28 体检 P2；2026-11-15 S6.8 收紧）。
 *
 * 部署会换掉带哈希的产物名，旧标签页里那些**还没加载过**的页面块就 404 了。
 * Vite 对这种情况会抛 `vite:preloadError` —— 这里拦下来、**限流重载**。
 *
 * ⚠️ 旧实现每次脚本启动都清标记，持久 404 会变成无限刷新。现在：
 * - 10 秒内最多自动重载一次；
 * - 第二次仍失败时不重载，让 lazy rejection 冒泡到 `ErrorBoundary` 显示可见错误；
 * - 网络/部署恢复后用户点「重新加载」即可；超过窗口后仍保留一次自愈机会。
 */
window.addEventListener('vite:preloadError', (event) => {
  // 阻止 Vite 把错误继续抛给上层（否则重载成功也会在控制台留一条红字）
  event.preventDefault()
  let storage: Storage | null = null
  try {
    storage = window.sessionStorage
  } catch {
    // 隐私模式下 sessionStorage 不可用：交给 ErrorBoundary 出按钮
  }
  if (decideChunkReload(storage) === 'reload') {
    window.location.reload()
  }
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
