/**
 * 全局股票研究侧栏的 Provider（S1-C2）。
 *
 * 职责：
 * - 保存「当前选中的股票 + Tab + 来源页 + 数据日期」；
 * - 与 URL 的 `?stock=` 双向同步（保留其它 query，不产生多余历史）；
 * - 通过 `BroadcastChannel` 做**最小**多窗口同步（只发代码 / Tab / 一个不可逆的
 *   账户作用域哈希，不发 Cookie、Token、账号明文或持仓）；
 * - 登录态变化时清空选择与缓存（换账户不继承上一账户的数据）。
 *
 * 设计取舍：
 * - URL 里的 `stock` 是**唯一可分享入口**，刷新后能恢复；
 * - 打开 / 关闭各只推一条历史，浏览器前进后退与侧栏状态自然同步；
 * - 广播只由用户动作触发，收到广播后不再回广播 —— 两个窗口不会互相刷消息。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import { useAuth } from './authContext'
import { clearResearchCache } from './stockResearchCache'
import {
  isResearchTab,
  normalizeStockCode,
  StockResearchContext,
} from './stockResearchContext'
import type {
  OpenResearchContext,
  ResearchSelection,
  ResearchTab,
  StockResearchValue,
} from './stockResearchContext'

const CHANNEL_NAME = 'zhifujing:stock-research'
const SYNC_KEY = 'stock-research:multi-window'
const STOCK_PARAM = 'stock'

const EMPTY: ResearchSelection = {
  code: null,
  sourcePage: null,
  selectedAsOfDate: null,
  open: false,
  activeTab: 'overview',
}

/**
 * 不可逆的账户作用域：只用于「同账户窗口之间」比对，不携带可读账号信息。
 *
 * ⚠️ 这是 **djb2 非加密散列**，只够做「两个窗口是不是同一个登录身份」的比对，
 * 不具备密码学隔离强度。真正的访问控制依赖后端登录鉴权；即使哈希被碰撞，
 * 也只是多打开一个侧栏，拿不到别人账户的数据（请求仍带各自的 Cookie）。
 */
function accountScope(id: number | null, username: string | null): string {
  const raw = `${id ?? ''}:${username ?? ''}`
  let hash = 5381
  for (let index = 0; index < raw.length; index += 1) {
    hash = ((hash << 5) + hash + raw.charCodeAt(index)) >>> 0
  }
  return hash.toString(36)
}

function readSyncPreference(): boolean {
  try {
    const raw = localStorage.getItem(SYNC_KEY)
    // 默认开：多窗口同步是「可预期的默认行为」，想关的人在侧栏底部关掉即可
    return raw == null ? true : raw === '1'
  } catch {
    return true
  }
}

export function StockResearchProvider({ children }: { children: ReactNode }) {
  const { me, checking } = useAuth()
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const [selection, setSelection] = useState<ResearchSelection>(EMPTY)
  const [multiWindowSync, setMultiWindowSyncState] = useState(readSyncPreference)

  const userId = me?.id ?? null
  const scope = useMemo(
    () => accountScope(userId, me?.username ?? null),
    [userId, me?.username],
  )

  // 广播只在用户动作里发；收到广播后只更新本地状态与 URL，不再回广播（防循环）
  const lastUserId = useRef<number | null | undefined>(undefined)
  const channelRef = useRef<BroadcastChannel | null>(null)
  /**
   * `useSearchParams` 返回的 setter 可能是每次渲染都新建的函数。
   * 直接把它写进 `useCallback` 依赖，会让 `writeUrl` → 广播通道的 effect
   * 每渲染一次就重建/关闭一次通道（表现为「消息发给了上一个已被关闭的实例」）。
   * 用 ref 固定住最新实现，回调本身保持稳定。
   */
  const setSearchParamsRef = useRef(setSearchParams)
  useEffect(() => {
    setSearchParamsRef.current = setSearchParams
  }, [setSearchParams])

  /** 只改 `stock` 参数，保留页面自己的筛选条件；值没变就不动历史 */
  const writeUrl = useCallback((code: string | null) => {
    setSearchParamsRef.current((prev) => {
      const next = new URLSearchParams(prev)
      if (code) next.set(STOCK_PARAM, code)
      else next.delete(STOCK_PARAM)
      return next.toString() === prev.toString() ? prev : next
    })
  }, [])

  const broadcast = useCallback(
    (payload: { type: 'open'; code: string; tab: ResearchTab } | { type: 'close' }) => {
      if (!multiWindowSync) return
      if (!scope) return
      const channel = channelRef.current
      if (!channel) return
      channel.postMessage({ ...payload, scope })
    },
    [multiWindowSync, scope],
  )

  const openStockResearch = useCallback(
    (rawCode: string, context: OpenResearchContext = {}) => {
      if (!me) return // 未登录不打开、更不请求个股数据
      const code = normalizeStockCode(rawCode)
      if (!code) return // 非法代码不产生请求
      const tab = context.tab && isResearchTab(context.tab) ? context.tab : 'overview'
      setSelection({
        code,
        sourcePage: context.sourcePage ?? location.pathname,
        selectedAsOfDate: context.asOfDate ?? null,
        open: true,
        activeTab: tab,
      })
      writeUrl(code)
      broadcast({ type: 'open', code, tab })
    },
    [me, location.pathname, writeUrl, broadcast],
  )

  const closeStockResearch = useCallback(() => {
    setSelection((prev) => ({ ...prev, open: false, code: null }))
    writeUrl(null)
    broadcast({ type: 'close' })
  }, [writeUrl, broadcast])

  const selectResearchTab = useCallback(
    (tab: ResearchTab) => {
      setSelection((prev) => (prev.open ? { ...prev, activeTab: tab } : prev))
      if (selection.code && selection.open) {
        broadcast({ type: 'open', code: selection.code, tab })
      }
    },
    [selection.code, selection.open, broadcast],
  )

  const setMultiWindowSync = useCallback((enabled: boolean) => {
    setMultiWindowSyncState(enabled)
    try {
      localStorage.setItem(SYNC_KEY, enabled ? '1' : '0')
    } catch {
      // 隐私模式下写不进去：当前会话内仍然按开关生效
    }
  }, [])

  // URL → 状态（刷新恢复、浏览器前进后退、外部链接）
  useEffect(() => {
    if (checking) return
    const code = normalizeStockCode(searchParams.get(STOCK_PARAM))
    if (!me) {
      // 未登录时 `?stock=` 不构成可恢复的研究状态：清掉 URL，别把别人的分享链接
      // 变成「登录后自动打开」的隐式行为
      if (code) writeUrl(null)
      setSelection((prev) => (prev.open || prev.code ? { ...EMPTY } : prev))
      return
    }
    setSelection((prev) => {
      if (!code) {
        if (!prev.open) return prev
        return { ...prev, open: false, code: null }
      }
      if (prev.open && prev.code === code) return prev
      return { ...prev, code, open: true }
    })
  }, [searchParams, me, checking, writeUrl])

  // 登录态变化：登出 / 换账户时清空侧栏与缓存
  useEffect(() => {
    if (checking) return
    const current = userId
    const previous = lastUserId.current
    if (previous !== undefined && previous !== current) {
      clearResearchCache()
      setSelection(EMPTY)
      writeUrl(null)
    }
    lastUserId.current = current
  }, [checking, userId, writeUrl])

  // 多窗口同步：只在登录后、开关打开时建立通道
  useEffect(() => {
    if (!me || !multiWindowSync || typeof BroadcastChannel === 'undefined') {
      return
    }
    const channel = new BroadcastChannel(CHANNEL_NAME)
    channelRef.current = channel
    channel.onmessage = (event: MessageEvent) => {
      const message = event.data as
        | { type?: string; code?: string; tab?: string; scope?: string }
        | null
      // 作用域不同 = 另一个账户的窗口，忽略（不回广播，避免循环）
      if (!message || message.scope !== scope) return
      if (message.type === 'open') {
        const code = normalizeStockCode(message.code)
        if (!code) return
        const tab = isResearchTab(message.tab) ? message.tab : 'overview'
        setSelection((prev) => ({
          ...prev,
          code,
          open: true,
          activeTab: tab,
        }))
        writeUrl(code)
      } else if (message.type === 'close') {
        setSelection((prev) => ({ ...prev, open: false, code: null }))
        writeUrl(null)
      }
    }
    return () => {
      channel.close()
      channelRef.current = null
    }
  }, [me, multiWindowSync, scope, writeUrl])

  const value = useMemo<StockResearchValue>(
    () => ({
      ...selection,
      openStockResearch,
      closeStockResearch,
      selectResearchTab,
      multiWindowSync,
      setMultiWindowSync,
    }),
    [
      selection,
      openStockResearch,
      closeStockResearch,
      selectResearchTab,
      multiWindowSync,
      setMultiWindowSync,
    ],
  )

  return (
    <StockResearchContext.Provider value={value}>{children}</StockResearchContext.Provider>
  )
}
