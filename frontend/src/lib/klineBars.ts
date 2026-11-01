/**
 * 日线 / 周线 / 月线 → 图上的 bar（2026-11-01 从 `components/KLineChart.tsx` 拆出）。
 *
 * 为什么单独一个文件：`KLineChart.tsx` 导出组件，同时导出这个转换函数会让
 * Fast Refresh 退化成整页重载（`react/only-export-components`）。这里是纯函数，
 * 不引 EChart，`lib/klinePeriod.ts` 直接用它。
 *
 * `KLineBar` 类型仍定义在 `components/KLineChart.tsx`（只被 import，不构成运行时依赖）：
 * 把它搬走要连带改十几处 `import type { KLineBar }`，收益只有「lib 不依赖 components」。
 */

import type { StockDailyRow } from '../api/types'
import type { KLineBar } from '../components/KLineChart'
import { fmtShortDate } from './format'

/**
 * 日线 / 周线 / 月线 → 图上的 bar（后端已经把周月聚合好了）。
 *
 * `withYear` 给周/月 K 用：它们看的是 2 年，横轴只写 `MM-DD` 会出现两个
 * `09-30`（2024 与 2025）分不清的情况。日线保持短标签，250 根挤得下。
 */
export function dailyBars(
  rows: StockDailyRow[],
  { withYear = false }: { withYear?: boolean } = {},
): KLineBar[] {
  return rows.map((row) => ({
    date: row.trade_date,
    label: withYear ? row.trade_date.slice(2) : fmtShortDate(row.trade_date),
    open: row.open,
    high: row.high,
    low: row.low,
    close: row.close,
    volume: row.volume,
    amount: row.amount,
    turnover: row.turnover,
    pct_chg: row.pct_chg,
    // 周/月由后端重采样，那边给的是 null（粒度上不成立）
    limitUp: row.is_limit_up === true,
    limitDown: row.is_limit_down === true,
  }))
}
