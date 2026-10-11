/** 数值与时间格式化。 */

const DASH = '—'

/**
 * 缺值判定：null / undefined 与**非有限数**（NaN、Infinity）都算缺值。
 *
 * 后端理论上只给 JSON 数字，但解析链路万一吐出 NaN（比如脏数据参与了除法），
 * 原来只判 null 会把「NaN%」「NaN亿」直接印到页面上 —— 非有限数一律走占位符。
 */
function isMissing(value: number | null | undefined): value is null | undefined {
  return value == null || !Number.isFinite(value)
}

/** 金额：自动降到亿 / 万。成交额动辄千亿，原值不可读。 */
export function fmtAmount(value: number | null | undefined): string {
  if (isMissing(value)) return DASH
  const abs = Math.abs(value)
  if (abs >= 1e12) return `${(value / 1e12).toFixed(2)}万亿`
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${(value / 1e4).toFixed(1)}万`
  return value.toFixed(0)
}

/** 涨跌幅：始终带符号，便于一眼分辨方向。 */
export function fmtPct(value: number | null | undefined, digits = 2): string {
  if (isMissing(value)) return DASH
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}%`
}

export function fmtNum(
  value: number | null | undefined,
  digits = 2,
  suffix = '',
): string {
  if (isMissing(value)) return DASH
  return `${value.toFixed(digits)}${suffix}`
}

export function fmtInt(value: number | null | undefined): string {
  if (isMissing(value)) return DASH
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
 * ISO 时间戳 → `09-18 15:05`（不含年份、不含秒）。
 *
 * 用正则截字符串而不是 `new Date(value)`：后端给的是**不带时区**的本地时间串
 * （`2026-09-18T15:05:00`），交给 `Date` 解析会按运行环境时区偏移，跨时区看就对不上。
 * 页面里所有「几分钟前」级别的展示都用这个粒度，年份是噪音。
 */
export function fmtDateTime(value: string | null | undefined): string {
  if (!value) return DASH
  const matched = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/)
  if (!matched) return value
  const [, , month, day, hour, minute] = matched
  return `${month}-${day} ${hour}:${minute}`
}

/**
 * A 股惯例：红涨绿跌。
 * 0、空值与非有限数走中性色，不要误染色（NaN > 0 === false 会被染成跌色）。
 */
export function toneOf(value: number | null | undefined): string {
  if (isMissing(value) || value === 0) return 'text-fg-muted'
  return value > 0 ? 'text-up' : 'text-down'
}

/** 涨跌停家数对比：用色条长度表达多空强弱。 */
export function breadthRatio(
  up: number | null | undefined,
  down: number | null | undefined,
): number {
  const u = up ?? 0
  const d = down ?? 0
  // 非有限数（NaN/Infinity）或两边都没数据时给中性的一半，别让色条算出 NaN 宽度
  if (!Number.isFinite(u) || !Number.isFinite(d) || u + d === 0) return 0.5
  return u / (u + d)
}
