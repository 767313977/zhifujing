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

/**
 * `toFixed` 的**负零归一**版本（2026-10-28 体检）。
 *
 * `(-0.001).toFixed(2)` 是 `"-0.00"`、`(-0.4).toFixed(0)` 是 `"-0"` —— 页面上「-0.00%」
 * 会被读成「跌了一点点」，其实是「就是 0」（涨跌幅四舍五入到分位后为 0）。
 * 这里统一还原成 `"0.00"` / `"0"`。
 */
function fixed(value: number, digits: number): string {
  const rounded = Number(value.toFixed(digits))
  return (Object.is(rounded, -0) ? 0 : rounded).toFixed(digits)
}

/**
 * 金额：自动降到亿 / 万。成交额动辄千亿，原值不可读。
 *
 * ⚠️ 档位按**四舍五入之后**的数字再判一次（2026-10-28 体检）：9,999,999,999 元按
 * 「亿」保留两位会进位成「100.00亿」，还不要紧；但 99,999,999 元按「万」保留一位是
 * 「10000.0万」—— 它已经等于 1 亿了，档位与数字自相矛盾。所以进位到下一档时换档。
 */
export function fmtAmount(value: number | null | undefined): string {
  if (isMissing(value)) return DASH
  const abs = Math.abs(value)
  if (abs >= 1e8) {
    const text = fixed(value / 1e8, 2)
    if (Math.abs(Number(text)) >= 1e4) return `${fixed(value / 1e12, 2)}万亿`
    return `${text}亿`
  }
  if (abs >= 1e4) {
    const text = fixed(value / 1e4, 1)
    if (Math.abs(Number(text)) >= 1e4) return `${fixed(value / 1e8, 2)}亿`
    return `${text}万`
  }
  return fixed(value, 0)
}

/** 涨跌幅：始终带符号，便于一眼分辨方向。 */
export function fmtPct(value: number | null | undefined, digits = 2): string {
  if (isMissing(value)) return DASH
  return `${value >= 0 ? '+' : ''}${fixed(value, digits)}%`
}

/**
 * 回撤：**0 不加 `+`**。
 *
 * 回撤按定义 ≤ 0，唯一会走到「非负」的情形就是 0（持仓期间没跌过）。
 * 用 `fmtPct` 会显示成 `+0.00%`，看着像一笔收益（2026-10-21 清单 #3）。
 */
export function fmtDrawdown(value: number | null | undefined, digits = 2): string {
  if (isMissing(value)) return DASH
  return `${value > 0 ? '+' : ''}${fixed(value, digits)}%`
}

export function fmtNum(
  value: number | null | undefined,
  digits = 2,
  suffix = '',
): string {
  if (isMissing(value)) return DASH
  return `${fixed(value, digits)}${suffix}`
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
 * 交易所板块。**依据是代码号段**（不是名字）：
 * `30x` 创业板、`68x` 科创板、`4xx/8xx/92x` 北交所，其余 `60x/00x` 归主板。
 *
 * 与后端 `scan_patterns.is_wudao_board`（悟道六池的板块关 = 创业板 + 科创板）
 * 以及 `LimitPoolBoard.marketOf`（同一号段口径的单字缩写版）用的是同一套依据。
 */
export type Board = '主板' | '创业板' | '科创板' | '北交所'

export function boardOf(code: string): Board | null {
  if (code.startsWith('30')) return '创业板'
  if (code.startsWith('68')) return '科创板'
  if (code.startsWith('4') || code.startsWith('8') || code.startsWith('92')) return '北交所'
  if (code.startsWith('60') || code.startsWith('00')) return '主板'
  return null
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

/** `Date` → 本地 `YYYY-MM-DD`。
 *
 * ⚠️ 不用 `toISOString().slice(0,10)`：那是按 **UTC** 切的，东八区凌晨 0~8 点
 * 会被算成**前一天**（2026-09-27 修）。
 */
export function localDate(value: Date): string {
  const pad = (number: number) => String(number).padStart(2, '0')
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`
}

/**
 * 今天的本地日期 `YYYY-MM-DD`，用作表单里日期字段的默认值。
 *
 * Journal / Plans 两个页面原先各有一份同样的实现（2026-10-22 合并到这里），
 * 口径与 `localDate` 一致 —— 同样是**本地**日期，不是 UTC。
 */
export function todayLocal(): string {
  return localDate(new Date())
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
