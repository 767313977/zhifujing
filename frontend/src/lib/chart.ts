/** 图表样式基座：与全局设计令牌保持一致，避免各图各写一套。
 *
 *  ⚠️ 这里是 `index.css` 里那几个 `--color-*` 的**副本**（canvas 读不到 CSS 变量，
 *  只能写死）。改那边就得同步改这边，尤其是 `fgDim` —— 它是坐标轴、图例那层
 *  小灰字的颜色，两边不一致的话，图里的字会比页面上的字暗一档。 */
export const CHART = {
  up: '#ff4d4f',
  down: '#00b96b',
  accent: '#ffb020',
  fg: '#e6e8ec',
  fgMuted: '#98a1af',
  // 与 --color-fg-dim 同步：2026-09-22 从 #59616d 提亮、2026-09-25 再提到 #87919f
  fgDim: '#87919f',
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

// 图表数字/文字字体：与 index.css 的 --font-mono 同栈（2026-09-25 用户指定）。
// Times New Roman 优先命中数字/英文，中文回退到仿宋。canvas 读不到 CSS 变量，只能写死。
const NUM_FONT =
  "'Times New Roman', 'FangSong_GB2312', '仿宋_GB2312', 'FangSong', '仿宋', serif"

export const AXIS_LABEL = {
  color: CHART.fgDim,
  fontSize: 11,
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
  backgroundColor: 'rgba(8, 10, 12, 0.94)',
  borderColor: CHART.line,
  borderWidth: 1,
  padding: [8, 10] as [number, number],
  textStyle: { color: CHART.fg, fontSize: 12, fontFamily: NUM_FONT },
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
