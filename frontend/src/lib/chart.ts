/** 图表样式基座：与全局设计令牌保持一致，避免各图各写一套。
 *
 *  ⚠️ 这里是 `index.css` 里那几个 `--color-*` 的**副本**（canvas 读不到 CSS 变量，
 *  只能写死）。改那边就得同步改这边，尤其是 `fgDim` —— 它是坐标轴、图例那层
 *  小灰字的颜色，两边不一致的话，图里的字会比页面上的字暗一档。
 *
 *  2026-10-13 全站改浅色（用户「黑色的看不清」），这里整份跟着换。**取值理由与
 *  对比度实测都写在 `index.css` 的令牌注释里**，不要在本地另立一套解释。
 */
export const CHART = {
  // 涨跌（浅底版：红涨绿跌，理由与对白对比度见 index.css）
  up: '#d92d20',
  down: '#0a7d4a',
  accent: '#1b64da',
  // 涨停 / 跌停标记：把那一根蜡烛整个换色，用来与普通涨跌柱分开。
  // 深色版是「金 + 绿」（金 #e0b055 做涨停、绿 #22b573 做跌停）：那对色在深底上与
  // 红/青分得开。浅色版**不能再用强调色**（它已从金改成蓝，涨停蜡烛画成蓝柱会跟
  // 「红涨」直接打架），所以两个标记都改成**同色相再深一档**：涨用深红、跌用深绿，
  // 压在普通柱上仍一眼看出是「更重的那根」，又不会引入第三种语义。
  limitUp: '#a11712', // 对白 7.9:1，比普通涨色 #d92d20 深一档
  limitDown: '#05603a', // 对白 8.4:1，比普通跌色 #0a7d4a 深一档
  fg: '#1f2328',
  fgMuted: '#57606a',
  fgDim: '#6e7781',
  line: '#d8dee6', // 与 --color-line 同步
  soft: '#eaeef3', // 虚线网格：与 --color-line-soft 同步
  ink: '#ffffff', // 与 --color-ink-900 同步（面板底）
  page: '#ffffff', // 与 --color-ink-950 同步（图区底；浅色下取白，与卡片同底）
} as const

/**
 * 把 `CHART` 里的 hex 变成带透明度的 rgba 字符串。
 *
 * 为什么要有它：图表里常要「同一个颜色但淡一点」（板块热力蒙层、情绪周期的中性柱）。
 * 以前这几处各自手写 `rgba(255,77,79,0.55)` 这种字面量，等于又多出好几份副本 ——
 * 而且**按 hex 搜是搜不到的**，改色时连续两轮都漏了：K 线成交量柱那道
 * `rgba(255,77,79)` 一直停在最早的荧光红，用户看个股页才发现。
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
 * 2026-09-25 改版：整组降饱和，并把**亮度对齐**（亮度不齐时，偏亮的那条线会凭「跳」
 * 抢注意力，读者会以为它更重要，而那只是配色造成的错觉）。
 * 2026-10-13 浅底版：整组**压深一档** —— 深色版那组中间调是按深底选的，铺在白底上
 * 会发飘；压深后对白 4.1~4.6:1（线条不需要文字那个 4.5:1 门槛，但太浅就看不见）。
 * 亮度仍保持对齐（相对亮度极差 0.035、占均值约 20%，与上一版同量级）。
 *
 * 2026-10-16（B 档 · 指数扩容）：**8 → 12 条**。指数走势面板要同时画 12 条指数线
 * （新增沪深300 / 上证50 / 中证500 / 中证2000 / 中证全指 / 平均股价），8 色会循环撞色。
 * **只在末尾追加**4 个（赭 / 橄榄 / 品红 / 石青），前 8 个下标不动 —— 其它面板都是按
 * 固定下标取色（`SERIES_PALETTE[0]` 是「金」这类语义），改前面的顺序会连带换色。
 * 新色同样按「降饱和 + 亮度对齐」选，对白 4.3~4.7:1。
 */
export const SERIES_PALETTE = [
  '#8a6d28', // 金
  '#4776b5', // 蓝
  '#8a68ad', // 紫
  '#3d817e', // 青
  '#a05f7d', // 粉
  '#6873b0', // 靛
  '#3a7fa8', // 天蓝
  '#6e7b8a', // 灰蓝
  '#9c6a48', // 赭
  '#7d7940', // 橄榄
  '#a3659a', // 品红
  '#5f8290', // 石青
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
  // 浅底版：白底 + 发丝边 + 轻阴影。深色版那块 94% 黑底压在浅色页面上会像补丁，
  // 而且与卡片的边界看不清。圆角仍为 0，与全站「锐利边缘」的基调一致。
  backgroundColor: '#ffffff',
  borderColor: CHART.line,
  borderWidth: 1,
  padding: [8, 10] as [number, number],
  textStyle: { color: CHART.fg, fontSize: 13, fontFamily: NUM_FONT },
  extraCssText: 'border-radius:0;box-shadow:0 6px 20px rgba(15,23,42,0.14)',
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
