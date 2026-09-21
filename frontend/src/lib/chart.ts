/** 图表样式基座：与全局设计令牌保持一致，避免各图各写一套。 */

export const CHART = {
  up: '#ff4d4f',
  down: '#00b96b',
  accent: '#ffb020',
  fg: '#e6e8ec',
  fgMuted: '#8a93a0',
  fgDim: '#59616d',
  line: '#232a33',
  soft: '#191e25',
  ink: '#0c0f13',
} as const

/**
 * 多序列配色：不用红绿（那两个颜色在本站有涨跌含义），改用中性区分度高的色相。
 * 个数必须 ≥ 后端 `COMPARE_LIMIT`（8），否则第 9 条线会绕回去和第一条同色。
 */
export const SERIES_PALETTE = [
  '#ffb020', // 金
  '#5b9dff', // 蓝
  '#c084fc', // 紫
  '#40c4c4', // 青
  '#f472b6', // 粉
  '#818cf8', // 靛
  '#38bdf8', // 天蓝
  '#94a3b8', // 灰蓝
]

const MONO = 'Cascadia Mono, Consolas, monospace'

export const AXIS_LABEL = {
  color: CHART.fgDim,
  fontSize: 11,
  fontFamily: MONO,
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
  backgroundColor: 'rgba(8, 10, 12, 0.94)',
  borderColor: CHART.line,
  borderWidth: 1,
  padding: [8, 10] as [number, number],
  textStyle: { color: CHART.fg, fontSize: 12, fontFamily: MONO },
  extraCssText: 'border-radius:0;box-shadow:0 6px 24px rgba(0,0,0,0.5)',
}

export const LEGEND = {
  textStyle: { color: CHART.fgMuted, fontSize: 11 },
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
