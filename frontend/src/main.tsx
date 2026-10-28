import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import ErrorBoundary from './components/ErrorBoundary.tsx'

/**
 * 代码块（chunk）加载失败的一次性自愈（2026-10-28 体检 P2）。
 *
 * 部署会换掉带哈希的产物名，旧标签页里那些**还没加载过**的页面块就 404 了。
 * Vite 对这种情况会抛 `vite:preloadError` —— 这里拦下来、**重载一次**，把新的
 * `index.html` 与产物拿下来。用 `sessionStorage` 打标，避免「重载后还是坏」时
 * 变成无限刷新（那种情况交给 `ErrorBoundary` 显示「重新加载」按钮）。
 */
const RELOAD_FLAG = 'fupan:chunk-reload'

window.addEventListener('vite:preloadError', (event) => {
  // 阻止 Vite 把错误继续抛给上层（否则重载成功也会在控制台留一条红字）
  event.preventDefault()
  try {
    if (sessionStorage.getItem(RELOAD_FLAG)) return
    sessionStorage.setItem(RELOAD_FLAG, '1')
  } catch {
    // 隐私模式下 sessionStorage 可能不可用：不放标记也不重载，
    // 让 ErrorBoundary 出按钮，别在这里制造刷新循环
    return
  }
  window.location.reload()
})

// 正常渲染成功后清掉标记，下次部署还能再自愈一次
try {
  sessionStorage.removeItem(RELOAD_FLAG)
} catch {
  // 同上，忽略
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
