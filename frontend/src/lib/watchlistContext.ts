/**
 * 自选名单的 context 与读取钩子（2026-11-01 从 `lib/watchlist.tsx` 拆出）。
 *
 * 与 `lib/authContext.ts` 同一个理由：Provider 与钩子同文件导出会让 Fast Refresh
 * 退化成整页重载。这里只放 context + 类型 + 钩子，Provider 仍在 `lib/watchlist.tsx`。
 */

import { createContext, useContext } from 'react'

export interface WatchlistValue {
  /** 自选代码集合。命中判断用 `isWatched`，不要直接读这个集合 */
  codes: Set<string>
  isWatched: (code: string) => boolean
  /** 手动重拉（一般不用调，增删事件会自动触发） */
  refresh: () => Promise<void>
}

export const WatchlistContext = createContext<WatchlistValue | null>(null)

export function useWatchlist(): WatchlistValue {
  const value = useContext(WatchlistContext)
  if (value === null) {
    throw new Error('useWatchlist 必须在 WatchlistProvider 内使用')
  }
  return value
}
