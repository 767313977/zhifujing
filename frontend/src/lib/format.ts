/** 数值与时间格式化。 */

const DASH = '—'

/** 金额：自动降到亿 / 万。成交额动辄千亿，原值不可读。 */
export function fmtAmount(value: number | null | undefined): string {
  if (value == null) return DASH
  const abs = Math.abs(value)
  if (abs >= 1e12) return `${(value / 1e12).toFixed(2)}万亿`
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${(value / 1e4).toFixed(1)}万`
  return value.toFixed(0)
}

/** 涨跌幅：始终带符号，便于一眼分辨方向。 */
export function fmtPct(value: number | null | undefined, digits = 2): string {
  if (value == null) return DASH
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}%`
}

export function fmtNum(
  value: number | null | undefined,
  digits = 2,
  suffix = '',
): string {
  if (value == null) return DASH
  return `${value.toFixed(digits)}${suffix}`
}

export function fmtInt(value: number | null | undefined): string {
  if (value == null) return DASH
  return String(value)
}

/** 封板时间 093101 → 09:31:01 */
export function fmtSealTime(value: string | null | undefined): string {
  if (!value || value.length < 6) return DASH
  return `${value.slice(0, 2)}:${value.slice(2, 4)}:${value.slice(4, 6)}`
}

/** 2026-09-17 → 09-17 */
export function fmtShortDate(value: string | null | undefined): string {
  if (!value) return DASH
  return value.slice(5)
}

/**
 * A 股惯例：红涨绿跌。
 * 0 与空值走中性色，不要误染色。
 */
export function toneOf(value: number | null | undefined): string {
  if (value == null || value === 0) return 'text-fg-muted'
  return value > 0 ? 'text-up' : 'text-down'
}

/** 涨跌停家数对比：用色条长度表达多空强弱。 */
export function breadthRatio(
  up: number | null | undefined,
  down: number | null | undefined,
): number {
  const u = up ?? 0
  const d = down ?? 0
  if (u + d === 0) return 0.5
  return u / (u + d)
}
