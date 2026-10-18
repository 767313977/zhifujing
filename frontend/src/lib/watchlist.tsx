/**
 * 自选股名单（全站共享一份）。
 *
 * 为什么要放在 context：`StockLink` 是全站统一的个股入口（十几个列表都在用），
 * 每个列表各拉一次 `/watchlist` 会重复几十个请求；更要紧的是「加/删自选之后，
 * 别的页面上的 ★ 还是旧的」。这里登录后拉一次，增删时由 api 层发事件触发刷新。
 *
 * 未登录不请求：`me` 为空时直接清空名单。登录页不渲染任何 `StockLink`，
 * 所以不会出现「没有 Provider 就报错」的情况。
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api, WATCHLIST_CHANGED_EVENT } from '../api/client'
import { useAuth } from './auth'

interface WatchlistValue {
  /** 自选代码集合。命中判断用 `isWatched`，不要直接读这个集合 */
  codes: Set<string>
  isWatched: (code: string) => boolean
  /** 手动重拉（一般不用调，增删事件会自动触发） */
  refresh: () => Promise<void>
}

const WatchlistContext = createContext<WatchlistValue | null>(null)

export function WatchlistProvider({ children }: { children: ReactNode }) {
  const { me, checking } = useAuth()
  const [codes, setCodes] = useState<Set<string>>(() => new Set())

  const refresh = useCallback(async () => {
    if (!me) {
      setCodes(new Set())
      return
    }
    try {
      const rows = await api.watchlist()
      setCodes(new Set(rows.map((row) => row.code)))
    } catch {
      // 自选高亮是增强项：失败就当作「没有自选」，不能影响页面主体
    }
  }, [me])

  // 首次确认登录态之后再拉；登出时 `refresh` 会走上面的清空分支
  useEffect(() => {
    if (!checking) void refresh()
  }, [checking, refresh])

  useEffect(() => {
    const onChange = () => {
      void refresh()
    }
    window.addEventListener(WATCHLIST_CHANGED_EVENT, onChange)
    return () => window.removeEventListener(WATCHLIST_CHANGED_EVENT, onChange)
  }, [refresh])

  const value = useMemo<WatchlistValue>(
    () => ({
      codes,
      isWatched: (code: string) => codes.has(code),
      refresh,
    }),
    [codes, refresh],
  )

  return <WatchlistContext.Provider value={value}>{children}</WatchlistContext.Provider>
}

export function useWatchlist(): WatchlistValue {
  const value = useContext(WatchlistContext)
  if (value === null) {
    throw new Error('useWatchlist 必须在 WatchlistProvider 内使用')
  }
  return value
}
