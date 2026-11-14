/**
 * 侧栏的按需挂载（S1-C / D2）。
 *
 * `StockResearchDrawer` 依赖 ECharts 与 K 线图，直接静态 import 会把这一大块并进
 * 每个页面都要加载的 `Layout` 公共块。这里只做一件事：**打开侧栏时才加载抽屉代码**，
 * 首屏不付这份体积。
 */

import { Suspense, lazy } from 'react'
import { useStockResearch } from '../lib/stockResearchContext'

const Drawer = lazy(() => import('./StockResearchDrawer'))

export default function StockResearchHost() {
  const { open } = useStockResearch()
  if (!open) return null
  return (
    <Suspense fallback={null}>
      <Drawer />
    </Suspense>
  )
}
