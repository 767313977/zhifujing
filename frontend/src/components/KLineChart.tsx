import { useMemo } from 'react'
import type { StockDailyRow } from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import { AXIS_LABEL, CHART, TOOLTIP } from '../lib/chart'
import { fmtAmount, fmtPct, fmtShortDate } from '../lib/format'

const MA_WINDOWS = [5, 10, 20]
const MA_COLORS = ['#b8944f', '#6f93c4', '#a583c4']

/**
 * 副图均量线（成交量自己的均线）。
 *
 * 配色**复用主图均线的色**（MAVOL5 = MA5 的金、MAVOL10 = MA10 的蓝）—— 同一个颜色就
 * 代表「同一个周期的均线」，副图这两条只是量的版本。
 *
 * ⚠️ 原来用的是同花顺默认的纯黄 `#ffff00` / 品红 `#ff00ff`，2026-09-26 改掉，原因两条：
 * 1. **它俩是整张图里最扎眼的元素**（纯黄在深底上比涨跌色还亮），把注意力从 K 线上抢走；
 * 2. **品红那条在依据上就不成立** —— 用户给的同花顺截图里只有一条黄线（x 覆盖
 *    2165/2469 列，据此才认出来的），品红是按「同花顺默认」补的，没有实据。
 */
const VOL_MA = [
  { window: 5, color: MA_COLORS[0] },
  { window: 10, color: MA_COLORS[1] },
]

/**
 * 星期几。日期串补 `T00:00:00` 再取**本地**星期 —— 直接 `new Date('2026-09-24')`
 * 会按 UTC 解析，东八区在凌晨那几个小时会算错一天。
 */
const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

function weekdayOf(date: string): string {
  const day = new Date(`${date}T00:00:00`)
  return Number.isNaN(day.getTime()) ? '' : WEEKDAYS[day.getDay()]
}

/**
 * tooltip 里的一行：左边中文标签（次文字色）、右边数值（等宽、右对齐）。
 *
 * 用 `table` 而不是 flex：tooltip 是 ECharts 注入的裸 HTML 片段，表格自带列对齐，
 * 不用再依赖样式表 —— 而两列**在不同行之间必须严格对齐**，否则数值会参差。
 */
function tipRow(label: string, value: string, color: string = CHART.fg): string {
  return (
    `<tr><td style="padding:1px 10px 1px 0;color:${CHART.fgMuted}">${label}</td>` +
    `<td style="padding:1px 0;text-align:right;font-variant-numeric:tabular-nums;` +
    `color:${color}">${value}</td></tr>`
  )
}

/**
 * K 线 tooltip 的内容，按**同花顺**那张图的样子排（用户 2026-09-26 要求）。
 *
 * 原先交给 ECharts 自动罗列所有序列，结果是：标题用英文键名（`open` / `close` /
 * `lowest` / `highest`）、成交量和 K 线分成两段各带一个日期、MA 值也堆在里面 ——
 * 一屏十几行。现在收成「一行一天」的字段表，字段与同花顺对齐：
 * 开盘价 / 最高价 / 最低价 / 收盘价 / 涨幅 / 振幅 / 成交量 / 成交额 / 换手 / 开盘涨幅。
 *
 * ⚠️ **查不到的就不显示**（用户明确要求）。两类情况整行不出现，而不是填 0 或「—」：
 * - `盘后量 / 盘后额`：我们没有任何数据源，直接不做
 * - 窗口第一根没有前一根收盘价 → `振幅` 与 `开盘涨幅` 算不出来
 */
function tipHtml(bars: KLineBar[], index: number): string {
  const bar = bars[index]
  if (!bar) return ''
  const prev = bars[index - 1]?.close ?? null
  const rows: string[] = []
  const add = (label: string, value: string | null, color?: string) => {
    if (value !== null) rows.push(tipRow(label, value, color))
  }
  /** 价格：两位小数；缺值给 null（= 这一行不显示） */
  const price = (value: number | null) => (value == null ? null : value.toFixed(2))

  add('开盘价', price(bar.open))
  add('最高价', price(bar.high))
  add('最低价', price(bar.low))
  add('收盘价', price(bar.close))
  add(
    '涨幅',
    bar.pct_chg == null ? null : fmtPct(bar.pct_chg),
    (bar.pct_chg ?? 0) >= 0 ? CHART.up : CHART.down,
  )
  add(
    '振幅',
    prev && prev > 0 && bar.high != null && bar.low != null
      ? `${(((bar.high - bar.low) / prev) * 100).toFixed(2)}%`
      : null,
  )
  add('成交量', bar.volume == null ? null : `${fmtAmount(bar.volume)}股`)
  add('成交额', bar.amount == null ? null : fmtAmount(bar.amount))
  add('换手', bar.turnover == null ? null : `${bar.turnover.toFixed(2)}%`)
  add(
    '开盘涨幅',
    prev && prev > 0 && bar.open != null ? fmtPct((bar.open / prev - 1) * 100) : null,
    bar.open != null && prev != null && bar.open >= prev ? CHART.up : CHART.down,
  )

  return (
    `<div>${bar.date} ${weekdayOf(bar.date)}</div>` +
    `<table style="border-collapse:collapse;margin-top:2px">${rows.join('')}</table>`
  )
}

/** 同花顺的网格是淡实线，不是本站其它图那种虚线。 */
const THS_SPLIT_LINE = {
  show: true,
  lineStyle: { color: CHART.line, type: 'solid' as const },
}

/**
 * 涨停那天的蜡烛：**整根实心描金**（用户 2026-09-26 要的）。
 *
 * 判据在后端（`services/limit_rules.py`），前端只读 `is_limit_up` —— 板块限幅
 * （主板 10 / 创业板科创板 20 / 北交所 30 / 主板 ST 5）与「收盘价 = 当日最高价」
 * 这两个口径都留在后端一处，前端不重算。
 *
 * 四个键全给：ECharts 的 data-item `itemStyle` 会与序列级的合并，只覆盖想改的那几个也
 * 可以，但那样「阳线空心」的 `color: 'transparent'` 会漏过来 —— 涨停日要的恰恰是实心。
 *
 * 颜色复用**强调金**（与关键位虚线同色）：画法上分得开（一个是实心蜡烛、一个是水平虚线），
 * 且语义都是「这里要留意」，不再为它新增一个令牌。
 */
const LIMIT_UP_ITEM_STYLE = {
  color: CHART.accent,
  color0: CHART.accent,
  borderColor: CHART.accent,
  borderColor0: CHART.accent,
} as const

/**
 * 跌停那天的蜡烛：**整根实心绿**（用户 2026-09-26 要的，与涨停那套完全对称）。
 *
 * 绿而不是站点跌色（青 `#45bfc4`）—— 两个理由：一是标记本来就该与普通柱**分色**，
 * 涨停用金也是这个道理；二是青还是绿**本来就是两回事**，全站「红涨青跌」的文案说的
 * 是青，这根绿蜡烛是例外，不会读成「跌得更多」。
 *
 * 同上，四个键全给，否则「阳线空心」的 `transparent` 会漏过来 —— 而一字跌停
 * （开 = 收 = 最低）在 ECharts 眼里正是阳线，漏了就会画成一根空心柱。
 */
const LIMIT_DOWN_ITEM_STYLE = {
  color: CHART.limitDown,
  color0: CHART.limitDown,
  borderColor: CHART.limitDown,
  borderColor0: CHART.limitDown,
} as const

/**
 * 图上的一根 K。
 *
 * 三个周期的来源不同（日线读库、周/月由后端重采样），但画法完全一样，
 * 所以统一成这个形状再交给图 —— 组件不必知道自己画的是哪个周期。
 * `label` 是**已经格式化好的**横轴文字（日线 `MM-DD`，周/月 `YY-MM-DD`）。
 */
export interface KLineBar {
  /** 原始交易日 `YYYY-MM-DD`。**只有它带年份** —— `label` 是给横轴用的短标签，
   *  日线只有 `MM-DD`，拿它在 tooltip 里写日期会缺年份（跨年时认不出来） */
  date: string
  label: string
  open: number | null
  high: number | null
  low: number | null
  close: number | null
  volume: number | null
  /** 成交额（元）。周/月是组内求和 */
  amount: number | null
  /** 换手率（百分数）。**只有日线有** —— 周/月是几天合并，比率不可加 */
  turnover: number | null
  /** 决定成交量柱的颜色。周/月的**第一根**没有前一根可比，是 null
   *  （图上退回按「收 - 开」染色，与日线缺涨跌幅时的行为一致） */
  pct_chg: number | null
  /** 收盘涨停（只有日线会是 true；周/月恒为 false，见 `is_limit_up` 的说明） */
  limitUp: boolean
  /** 收盘跌停（同上，只有日线会是 true） */
  limitDown: boolean
}

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

/** 图上的关键位标记（形态的突破价 / 支撑价）。 */
export interface KeyLevel {
  /** 线的标签，如「突破 12.34」 */
  label: string
  value: number
  kind: 'breakout' | 'support'
}

/** 移动平均。窗口不足或含空值时给 null，ECharts 会自然断线。 */
function movingAverage(values: (number | null)[], window: number): (number | null)[] {
  return values.map((_, index) => {
    if (index + 1 < window) return null
    const slice = values.slice(index + 1 - window, index + 1)
    if (slice.some((value) => value == null)) return null
    const sum = slice.reduce<number>((acc, value) => acc + (value as number), 0)
    return Number((sum / window).toFixed(2))
  })
}

interface Props {
  bars: KLineBar[]
  height?: number
  /**
   * 形态关键位，画成水平虚线。
   *
   * 注意这些值是**前复权口径**的（引擎判定时用的就是复权序列），所以调用方
   * 必须同时用 `adjust=1` 取日线，否则除权股上的标注线会画在错误的高度 ——
   * 图和线对不上，看图确认就失去意义了。
   */
  keyLevels?: KeyLevel[]
}

/**
 * K 线图（蜡烛 + 均线 + 成交量），个股详情页与形态选股页共用。
 *
 * 2026-09-26 起画法按**同花顺**那套来：阳线空心红 / 阴线实心青、网格淡实线、
 * 图区比卡片沉一档、副图带均量线。配色直接吃站点令牌（`CHART` / `index.css`），
 * 不另立一套 —— 当天全站配色也换成了同花顺，两边本来就是同一组值。
 *
 * **涨停那天的蜡烛整根实心描金、跌停那天的整根实心绿**（日 K 才有，判据在后端，
 * 见 `LIMIT_UP_ITEM_STYLE` / `LIMIT_DOWN_ITEM_STYLE`）。
 *
 * 从 `StockDetail.tsx` 里抽出来而不是复制一份：这个图有三处容易写错的地方
 * —— A 股的红涨青跌覆盖（ECharts 默认是欧美惯例）、主图与副图的轴联动、
 * 成交量柱跟随涨跌染色 —— 复制出去迟早会有一份忘了改。
 *
 * 均线按**行数**滚动，所以周期切换后自动变成「5/10/20 周」「5/10/20 月」，
 * 不需要为每个周期另配参数。
 */
export default function KLineChart({ bars, height = 420, keyLevels = [] }: Props) {
  const option = useMemo<ChartOption>(() => {
    if (bars.length === 0) return {}
    const dates = bars.map((bar) => bar.label)
    const closes = bars.map((bar) => bar.close)
    const rawVolumes = bars.map((bar) => bar.volume)
    // ECharts 蜡烛图的数据顺序是 [开, 收, 低, 高]；涨跌停那天的整根实心换色，
    // 靠 data-item 上挂 itemStyle 覆盖序列级的画法（见 LIMIT_UP_ITEM_STYLE /
    // LIMIT_DOWN_ITEM_STYLE）。两者互斥，先判涨停
    const candles = bars.map((bar) => {
      const value = [bar.open, bar.close, bar.low, bar.high]
      if (bar.limitUp) return { value, itemStyle: LIMIT_UP_ITEM_STYLE }
      if (bar.limitDown) return { value, itemStyle: LIMIT_DOWN_ITEM_STYLE }
      return value
    })
    const volumes = bars.map((bar) => ({
      value: bar.volume,
      // 成交量柱跟随当日涨跌染色；涨跌幅缺失时用收盘价与开盘价比较。
      // 与蜡烛同一个红/青（同花顺在图上把柱子和蜡烛分了两个色阶，本站 2026-09-26
      // 统一取了亮的那档 —— 表格数字也是这两个值，全站一套）
      itemStyle: {
        color:
          (bar.pct_chg ?? (bar.close ?? 0) - (bar.open ?? 0)) >= 0
            ? CHART.up
            : CHART.down,
      },
    }))

    // 关键位标签的落位：**按价格从高到低排，左右两端交替**。
    // 为什么不能都放右端：关键位是算出来的，两个价位挨得近时标签会印在同一处叠字 ——
    // 实测 300350（2026-09-23）的「突破 4.65 / 突破 4.60」只差 0.05 元 ≈ 7.8px，
    // 而标签高 10px，垂直就叠了 2.2px。交替之后相邻价位必然分居左右，
    // 横向直接分开，**与价格差多少无关**。
    const marks = keyLevels
      .filter((level) => Number.isFinite(level.value))
      .slice()
      .sort((left, right) => right.value - left.value)
    const MARK_LABEL_POSITIONS = ['insideEndTop', 'insideStartTop'] as const
    const legend = [
      ...MA_WINDOWS.map((w) => `MA${w}`),
      '成交量',
      ...VOL_MA.map((ma) => `MAVOL${ma.window}`),
    ]
    // 横轴放几个标签要看标签有多长：周月是 `25-09-30`（8 字符，比日线的 `09-21`
    // 长），同一宽度下要少放几个，否则相邻标签会贴在一起
    const wideLabels = (dates[0] ?? '').length > 6
    const interval = Math.max(0, Math.floor(dates.length / (wideLabels ? 6 : 8)))

    return {
      grid: [
        // 图区铺「页面底」那一阶（比面板底更暗），同花顺那种图比卡片沉一档的观感。
        // 铺在 grid 上而不是画布整体背景：画布整块上底会跟容器的圆角/内边距打架
        { left: 8, right: 14, top: 34, height: '56%', containLabel: true, backgroundColor: CHART.page },
        {
          left: 8,
          right: 14,
          top: '76%',
          height: '16%',
          containLabel: true,
          backgroundColor: CHART.page,
        },
      ],
      legend: {
        top: 2,
        left: 8,
        icon: 'rect',
        itemWidth: 10,
        itemHeight: 10,
        itemGap: 14,
        textStyle: { color: CHART.fgMuted, fontSize: 12 },
        data: legend,
      },
      tooltip: {
        ...TOOLTIP,
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        // 自己排版（同花顺那种「一行一天」的字段表），不用 ECharts 的自动列表 ——
        // 见 tipHtml 的说明。两个 grid 共用一个 tooltip，取同一根柱子的下标即可
        formatter: (params) => {
          const list = Array.isArray(params) ? params : [params]
          const index = list[0]?.dataIndex
          return typeof index === 'number' ? tipHtml(bars, index) : ''
        },
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      xAxis: [
        {
          type: 'category',
          data: dates,
          gridIndex: 0,
          axisLabel: { ...AXIS_LABEL, interval },
          axisLine: { lineStyle: { color: CHART.line } },
          axisTick: { show: false },
        },
        {
          type: 'category',
          data: dates,
          gridIndex: 1,
          axisLabel: { show: false },
          axisLine: { lineStyle: { color: CHART.line } },
          axisTick: { show: false },
        },
      ],
      yAxis: [
        {
          scale: true,
          gridIndex: 0,
          axisLabel: AXIS_LABEL,
          splitLine: THS_SPLIT_LINE,
          axisLine: { show: false },
        },
        {
          gridIndex: 1,
          // 成交量轴标签走 fmtAmount 的亿/万口径（用户 2026-09-26 要求）。
          // 原样是 `1,000,000,000` 这种 13 个字符的裸数字，又长又读不出量级。
          // 与站点其它地方同一口径：个股概况的成交量就是 `fmtAmount(volume) + '股'`
          //（如「2786.0万股」），DDE 图的纵轴也是直接拿 fmtAmount。
          axisLabel: {
            ...AXIS_LABEL,
            fontSize: 11,
            formatter: (value: number) => `${fmtAmount(value)}股`,
          },
          // 十字光标贴在轴上的那枚数值标签也得同口径：实测它**不跟随** axisLabel.formatter，
          // 鼠标一扫就冒出 `574,377,224.20` 这种裸数字（还带两位小数）
          axisPointer: {
            label: {
              // 参数类型交给 TS 从上下文推断：ECharts 的 `value` 是 ScaleDataValue
              //（可能是 string / number / Date），写窄了编译不过
              formatter: (params) =>
                `${fmtAmount(typeof params.value === 'number' ? params.value : null)}股`,
            },
          },
          splitLine: { show: false },
          axisLine: { show: false },
        },
      ],
      series: [
        {
          type: 'candlestick' as const,
          name: 'K线',
          data: candles,
          xAxisIndex: 0,
          yAxisIndex: 0,
          itemStyle: {
            // ECharts 默认是欧美惯例（绿涨红跌），必须覆盖成 A 股的红涨青跌：
            // color = 阳线（收 ≥ 开），color0 = 阴线。
            //
            // 阳线填 `transparent` 就是同花顺那种**空心阳线** —— 只留红色描边、
            // 体内透出背景色。ECharts 里 color 支持任意 CSS 颜色，透明即空心，
            // 不需要画两条线去凑。阴线保持实心青。
            color: 'transparent',
            color0: CHART.down,
            borderColor: CHART.up,
            borderColor0: CHART.down,
          },
          // 蜡烛体宽。ECharts 默认是 `bandWidth / 2`（源码 candlestickLayout：
          // `mathMax(mathMin(bandWidth / 2, barMaxWidth), barMinWidth)`，两个边界默认
          // null / 1），而边框是 1px —— 所以**体宽掉到 2px 以下时空心就被边框吃满、
          // 看起来跟实心一样**。日线 250 根时默认只有 3px 上下（内部约 1px，几乎看不出）。
          // 提到 70% 后体宽约 4.2px、内部 2.2px，日线也认得出空心，且间隙更小、
          // 更接近同花顺那种密排观感。
          // ⚠️ 窄窗口下仍看不出（bandWidth 本身不足 2px，物理限制，调这个没用）。
          barWidth: '70%',
          // 关键位画成水平虚线。挂在蜡烛序列的 markLine 上，这样它自动跟随主图
          // 的坐标轴，价格尺度一变就跟着变，不用自己算位置
          markLine: marks.length
            ? {
                symbol: 'none',
                silent: true,
                label: {
                  position: 'insideEndTop' as const,
                  color: CHART.fgMuted,
                  fontSize: 11,
                  formatter: '{b}',
                },
                data: marks.map((level, index) => ({
                  yAxis: level.value,
                  name: `${level.label} ${level.value.toFixed(2)}`,
                  // 相邻价位分居左右两端（见上面 marks 的注释）
                  label: { position: MARK_LABEL_POSITIONS[index % MARK_LABEL_POSITIONS.length] },
                  lineStyle: {
                    color: level.kind === 'breakout' ? CHART.accent : CHART.fgDim,
                    type: 'dashed' as const,
                    width: 1,
                  },
                })),
              }
            : undefined,
        },
        ...MA_WINDOWS.map((window, index) => ({
          type: 'line' as const,
          name: `MA${window}`,
          data: movingAverage(closes, window),
          xAxisIndex: 0,
          yAxisIndex: 0,
          smooth: true,
          symbol: 'none' as const,
          lineStyle: { width: 1.2, color: MA_COLORS[index] },
          itemStyle: { color: MA_COLORS[index] },
        })),
        {
          type: 'bar' as const,
          name: '成交量',
          data: volumes,
          xAxisIndex: 1,
          yAxisIndex: 1,
          barMaxWidth: 8,
        },
        // 均量线排在建量柱之后 —— 后画的在上层，否则细线会被柱子盖掉
        ...VOL_MA.map((ma) => ({
          type: 'line' as const,
          name: `MAVOL${ma.window}`,
          data: movingAverage(rawVolumes, ma.window),
          xAxisIndex: 1,
          yAxisIndex: 1,
          smooth: true,
          symbol: 'none' as const,
          lineStyle: { width: 1, color: ma.color },
          itemStyle: { color: ma.color },
        })),
      ],
    }
  }, [bars, keyLevels])

  if (bars.length === 0) {
    return (
      <div
        className="flex items-center justify-center text-[13px] text-fg-dim"
        style={{ height }}
      >
        暂无日线数据
      </div>
    )
  }
  return <EChart option={option} height={height} />
}
