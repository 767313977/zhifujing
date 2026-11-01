/**
 * 恐惧贪婪的**分档判据与配色**（2026-11-01 从 `components/FearGreedBrief.tsx` 拆出）。
 *
 * 为什么单独一个文件：`FearGreedBrief.tsx` 还导出 `ScoreHeader` 等组件，而组件文件
 * 同时导出普通函数会让 Fast Refresh 退化成整页重载（`react/only-export-components`）。
 *
 * ⚠️ 这里**一个图表相关的东西都不能 import**（连 `EChart` 的类型都不行）——
 * 首页只渲染一行读数，不能被牵进整包 ECharts（见 `FearGreedBrief` 头部说明）。
 * 配色与分档判据只此一份，`FearGreedPanel` 反向从这里引用。
 */

import type { FearGreedBand } from '../api/types'
import { CHART } from './chart'

/** 五档配色：绿→灰→红，与全站「红涨绿跌」一致（恐慌=绿、贪婪=红）。 */
const BAND_COLOR: Record<string, string> = {
  // 极端两档用同色相再深一档（与 K 线里涨停/跌停标记同一套取色理由）：
  // 压在中间档上仍一眼看出是「更重的那一端」，又不引入第三种语义
  extreme_fear: CHART.limitDown,
  fear: CHART.down,
  neutral: CHART.fgDim,
  greed: CHART.up,
  extreme_greed: CHART.limitUp,
}

export function bandColor(key: string): string {
  return BAND_COLOR[key] ?? CHART.fgDim
}

/** 分数落在哪一档。返回该档（含后端给的中文名），分数为 null 时返回 null。 */
export function bandOf(bands: FearGreedBand[], score: number | null): FearGreedBand | null {
  if (score == null) return null
  // 与后端 `level_of` 同一判据：命中第一个「score <= upper」的档
  return bands.find((band) => score <= band.upper) ?? bands[bands.length - 1] ?? null
}

/** 读数色跟着分档走：红色贪婪 / 绿色恐慌，扫一眼就分得出方向。 */
export function scoreColor(bands: FearGreedBand[], score: number | null): string {
  const band = bandOf(bands, score)
  return band ? bandColor(band.key) : 'var(--color-fg-dim)'
}
