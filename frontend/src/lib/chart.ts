/** 图表样式基座：与全局设计令牌保持一致，避免各图各写一套。
 *
 *  ⚠️ 这里是 `index.css` 里那几个 `--color-*` 的**副本**（canvas 读不到 CSS 变量，
 *  只能写死）。改那边就得同步改这边，尤其是 `fgDim` —— 它是坐标轴、图例那层
 *  小灰字的颜色，两边不一致的话，图里的字会比页面上的字暗一档。 */
export const CHART = {
  // 涨跌：砖红 / 松绿。2026-09-25 用户反馈上一版「偏闷」，提亮提饱和到这两个值
  // （对比 5.55 / 7.01，之前是 4.68 / 4.88；亮度差 24% 是故意的，见 index.css 说明）
  up: '#f05a4d',
  down: '#22b573',
  accent: '#ddaa4d',
  fg: '#eae5da',
  fgMuted: '#a5a29b',
  fgDim: '#8a8881',
  line: '#2e2e31',
  soft: '#1f1f21',
  ink: '#131314', // 与 --color-ink-900 同步（面板底）
} as const

/**
 * 把 `CHART` 里的 hex 变成带透明度的 rgba 字符串。
 *
 * 为什么要有它：图表里常要「同一个颜色但淡一点」（K 线成交量柱、情绪周期的中性柱、
 * 板块热力蒙层）。以前这几处各自手写 `rgba(255,77,79,0.55)` 这种字面量，等于又多出
 * 好几份副本 —— 而且**按 hex 搜是搜不到的**，改色时连续两轮都漏了：
 * K 线成交量柱的 `rgba(255,77,79)` 一直停在最早的荧光红，用户看个股页才发现。
 * 统一走这里之后，改色只需要动 `CHART`。
 */
export function withAlpha(hex: string, alpha: number): string {
  const h = hex.replace('#', '')
  const r = parseInt(h.slice(0, 2), 16)
  const g = parseInt(h.slice(2, 4), 16)
  const b = parseInt(h.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

/**
 * 多序列配色：不用红绿（那两个颜色在本站有涨跌含义），改用中性区分度高的色相。
 * 个数必须 ≥ 后端 `COMPARE_LIMIT`（8），否则第 9 条线会绕回去和第一条同色。
 *
 * 2026-09-25 改版：整组降饱和，并把**亮度对齐**（极差 0.049，占均值 16.4% —— 旧配色
 * 是 57.3%）。亮度不齐的话，偏亮的那条线会凭「跳」抢注意力，读者会以为它更重要，
 * 而那只是配色造成的错觉。金色也从 #d3a559 压到 #b8944f 才落进这条带里。
 */
export const SERIES_PALETTE = [
  '#b8944f', // 金
  '#6f93c4', // 蓝
  '#a583c4', // 紫
  '#5f9f9d', // 青
  '#c18aa4', // 粉
  '#828cc0', // 靛
  '#639fc2', // 天蓝
  '#8b95a3', // 灰蓝
]

// 图表里的字：与 index.css 同栈。canvas 读不到 CSS 变量，只能把那一串写死 ——
// 改 index.css 的字体栈时**必须同步这里**，否则图里的字会退回 canvas 默认字体。
// 数字走等宽体（轴标签、tooltip 里的数值），未命中时回退到正文那套中文黑体。
const NUM_FONT =
  "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', 'Noto Sans SC', monospace"

export const AXIS_LABEL = {
  color: CHART.fgDim,
  // 12px：2026-09-25 跟页面文字一起 +1px（画布里的字不能靠 CSS 缩放，只能手改）
  fontSize: 12,
  fontFamily: NUM_FONT,
}

/** 统一的坐标轴样式：只留底部与左侧一条发丝线。 */
export const AXIS_LINE = {
  show: true,
  lineStyle: { color: CHART.line, width: 1 },
}

export const SPLIT_LINE = {
  show: true,
  lineStyle: { color: CHART.soft, type: 'dashed' as const },
}

export const TOOLTIP = {
  backgroundColor: 'rgba(12, 12, 13, 0.94)',
  borderColor: CHART.line,
  borderWidth: 1,
  padding: [8, 10] as [number, number],
  textStyle: { color: CHART.fg, fontSize: 13, fontFamily: NUM_FONT },
  extraCssText: 'border-radius:0;box-shadow:0 6px 24px rgba(0,0,0,0.5)',
}

export const LEGEND = {
  textStyle: { color: CHART.fgMuted, fontSize: 12 },
  itemWidth: 10,
  itemHeight: 10,
  itemGap: 14,
  icon: 'rect' as const,
}

export const GRID = {
  left: 8,
  right: 12,
  top: 28,
  bottom: 4,
  containLabel: true,
}
