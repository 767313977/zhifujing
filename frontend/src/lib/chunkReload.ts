/**
 * Chunk 加载失败的一次性自愈（2026-11-15 S6.8，从 `main.tsx` 抽出可测逻辑）。
 *
 * 部署会换掉带哈希的产物名，旧标签页里那些**还没加载过**的页面块会 404。
 * Vite 对这种情况会抛 `vite:preloadError`，之前的实现是「拦下来、重载一次」，
 * 并在**每次脚本启动时**清掉标记 —— 如果 chunk 是真的持久 404（不是换版本），
 * 就会变成无限刷新，用户永远看不到错误提示。
 *
 * 现在改为**时间窗口限流**：10 秒内最多自动重载一次；
 * 第二次仍失败时不重载，让 lazy 的 rejection 冒泡到根级 `ErrorBoundary`，
 * 用户能看到「页面加载失败 + 重新加载」的可见 fallback。
 * 网络/部署恢复后，点按钮即可正常加载；超过窗口后仍保留一次自愈机会。
 *
 * 纯函数形式便于单测：`now` / storage 都可注入，不直接触碰 `window.location`。
 */

export const CHUNK_RELOAD_KEY = 'fupan:chunk-reload'
export const CHUNK_RELOAD_WINDOW_MS = 10_000

export type ChunkReloadDecision = 'reload' | 'fallback'

interface ReloadStorage {
  getItem: (key: string) => string | null
  setItem: (key: string, value: string) => void
}

/**
 * @returns `reload`：可以自动重载一次；`fallback`：刚试过，交给 ErrorBoundary。
 *
 * 存储不可用（隐私模式 / 配额）时返回 `fallback`：无法记录「已经重载过」，
 * 再自动重载就有刷新循环风险，宁可显示错误按钮。
 */
export function decideChunkReload(
  storage: ReloadStorage | null,
  now: number = Date.now(),
): ChunkReloadDecision {
  if (storage === null) return 'fallback'
  try {
    const raw = storage.getItem(CHUNK_RELOAD_KEY)
    const last = raw === null ? 0 : Number(raw)
    if (Number.isFinite(last) && last > 0 && now - last < CHUNK_RELOAD_WINDOW_MS) {
      return 'fallback'
    }
    storage.setItem(CHUNK_RELOAD_KEY, String(now))
    return 'reload'
  } catch {
    return 'fallback'
  }
}
