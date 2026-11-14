/**
 * 全局股票研究侧栏的上下文（S1-C）。
 *
 * 与 `authContext` / `watchlistContext` 同一个拆分理由：组件（Provider）与
 * 类型 / 钩子分文件，避免 Fast Refresh 退化成整页重载。
 *
 * 默认值是**空实现**：列表页可能在测试或极少数未挂 Provider 的场景里渲染
 * 「快速研究」按钮，此时点击应当安静地什么都不做，而不是把整棵组件树打挂。
 */

import { createContext, useContext } from 'react'

export const RESEARCH_TABS = ['overview', 'kline', 'themes', 'limit', 'funds', 'lhb'] as const

export type ResearchTab = (typeof RESEARCH_TABS)[number]

export const RESEARCH_TAB_LABELS: Record<ResearchTab, string> = {
  overview: '概览',
  kline: 'K线',
  themes: '题材',
  limit: '涨停',
  funds: '资金',
  lhb: '龙虎榜',
}

export interface ResearchSelection {
  code: string | null
  sourcePage: string | null
  /**
   * **来源页面所选的观察日**（例如涨停复盘翻到 2026-09-24），不是 API 返回的
   * 数据日。侧栏里的多数只读接口只提供「最新本地资料」，所以绝不能用它冒充
   * 数据日；真正数据日由各自 Tab 从响应里读（见 `dataAsOfDate`）。
   */
  selectedAsOfDate: string | null
  open: boolean
  activeTab: ResearchTab
}

export interface OpenResearchContext {
  sourcePage?: string
  /** 页面当前观察日；仅用于提示「这是历史观察日」，不代表接口返回历史快照 */
  asOfDate?: string | null
  tab?: ResearchTab
}

export interface StockResearchValue extends ResearchSelection {
  openStockResearch: (code: string, context?: OpenResearchContext) => void
  closeStockResearch: () => void
  selectResearchTab: (tab: ResearchTab) => void
  /** 多窗口同步开关（默认开；关闭后仅当前窗口可用） */
  multiWindowSync: boolean
  setMultiWindowSync: (enabled: boolean) => void
}

const NOOP: StockResearchValue = {
  code: null,
  sourcePage: null,
  selectedAsOfDate: null,
  open: false,
  activeTab: 'overview',
  openStockResearch: () => {},
  closeStockResearch: () => {},
  selectResearchTab: () => {},
  multiWindowSync: false,
  setMultiWindowSync: () => {},
}

export const StockResearchContext = createContext<StockResearchValue>(NOOP)

export function useStockResearch(): StockResearchValue {
  return useContext(StockResearchContext)
}

/**
 * 统一标准化股票代码：只接受 6 位数字（A 股 / 北交所 / 科创板）。
 *
 * 非 6 位一律返回 null —— 侧栏**不会**为非法代码发请求。别在这里放宽成
 * 「有值就传」：`/api/stock/abc/daily` 会打到后端再 404，既慢又污染日志。
 */
export function normalizeStockCode(raw: string | null | undefined): string | null {
  const value = (raw ?? '').trim()
  if (!/^\d{6}$/.test(value)) return null
  return value
}

export function isResearchTab(value: unknown): value is ResearchTab {
  return typeof value === 'string' && (RESEARCH_TABS as readonly string[]).includes(value)
}
