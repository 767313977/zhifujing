import { useMemo } from 'react'
import type { StockDailyRow } from '../api/types'
import EChart from './EChart'
import type { ChartOption } from './EChart'
import { AXIS_LABEL, KLINE, TOOLTIP } from '../lib/chart'
import { fmtAmount, fmtPct, fmtShortDate } from '../lib/format'

/*
 * ⚠️ **这张图不画均线**（2026-10-08 用户要求「把均线去掉」：主图 MA5/10/20 与副图
 * 均量线 MAVOL5/10 一起去掉）。别再顺手加回来：
 *
 * - 用户的原话就是「把均线去掉」，截图里圈住的正是主图那三条线；
 * - 原来的两条均量线是照「同花顺默认」补的、没有实据（旧注释里写着），2026-09-26
 *   那轮配色只把它们调暗过，不如直接去掉；
 * - 均线的信息别处都有：指数条有 `MA5 上 / MA20 下`，形态选股有「均线多头排列」，
 *   而图上真正要看的是**关键位虚线**（`keyLevels`，形态引擎的前复权判定价）。
 */

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
 * K 线自己的轴文字与 tooltip —— 在 `AXIS_LABEL` / `TOOLTIP` 的**基础上只换颜色**。
 * 字号、字体族、内边距这些一律不另抄一遍：改了 `chart.ts` 那边，这里跟着走。
 *
 * 2026-10-15：这张图改用 `KLINE` 那套深色令牌（用户「配色改成图一」），
 * 其余 8 个图表仍然吃浅色的 `CHART`。
 */
const KLINE_AXIS_LABEL = { ...AXIS_LABEL, color: KLINE.fgDim }

const KLINE_TOOLTIP = {
  ...TOOLTIP,
  // 黑底图上那块白底 tooltip 会像补丁；换成比图区亮一档的深灰，
  // 边框用同一条网格色，与图一那种「浮在图上的一小块」观感一致
  backgroundColor: '#1b2229',
  borderColor: KLINE.line,
  textStyle: { ...TOOLTIP.textStyle, color: KLINE.fg },
  extraCssText: 'border-radius:0;box-shadow:0 6px 20px rgba(0,0,0,0.55)',
}

/**
 * tooltip 里的一行：左边中文标签（次文字色）、右边数值（等宽、右对齐）。
 *
 * 用 `table` 而不是 flex：tooltip 是 ECharts 注入的裸 HTML 片段，表格自带列对齐，
 * 不用再依赖样式表 —— 而两列**在不同行之间必须严格对齐**，否则数值会参差。
 */
function tipRow(label: string, value: string, color: string = KLINE.fg): string {
  return (
    `<tr><td style="padding:1px 10px 1px 0;color:${KLINE.fgMuted}">${label}</td>` +
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
    (bar.pct_chg ?? 0) >= 0 ? KLINE.upText : KLINE.downText,
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
    bar.open != null && prev != null && bar.open >= prev ? KLINE.upText : KLINE.downText,
  )

  return (
    `<div>${bar.date} ${weekdayOf(bar.date)}</div>` +
    `<table style="border-collapse:collapse;margin-top:2px">${rows.join('')}</table>`
  )
}

/**
 * 网格线。图一实测是 `#31393f` 的**点线** —— 在参考图上逐行扫过，连续段长 2~3px、
 * 约 4.7px 一个周期（旧注释写的是「同花顺是淡实线」，那是 2026-09-26 照另一张截图
 * 得出的结论，这次按用户给的图改成点线）。
 */
const THS_SPLIT_LINE = {
  show: true,
  lineStyle: { color: KLINE.line, type: [2, 2] },
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
 * 颜色用 `KLINE.limitUp`（**强调金**）：画法上分得开（一个是金色实心蜡烛、其它是
 * 红/青），语义上也不引入第三种颜色。
 *
 * ⚠️ 这个金是**深色版旧约定**。2026-10-13 全站改浅色时「金对白只有 2.2:1、当蜡烛色
 * 太浅」，才换成「同色相再深一档的涨色」；2026-10-15 K 线图改回深底（用户「配色改成
 * 图一」），金在黑底上又成立了，所以这档跟着回退。浅色那套 `CHART.limitUp` 仍在 ——
 * 别的图表将来要画涨停标记就用 `CHART` 那一份。
 */
const LIMIT_UP_ITEM_STYLE = {
  color: KLINE.limitUp,
  color0: KLINE.limitUp,
  borderColor: KLINE.limitUp,
  borderColor0: KLINE.limitUp,
} as const

/**
 * 跌停那天的蜡烛：**整根实心绿**（用户 2026-09-26 要的，与涨停那套完全对称）。
 *
 * 绿而不是站点跌色（青 `#17def1`，见 `KLINE.down`）—— 两个理由：一是标记本来就该与
 * 普通柱**分色**，涨停用金也是这个道理；二是青还是绿**本来就是两回事**，全站「红涨青跌」
 * 的文案说的是青，这根绿蜡烛是例外，不会读成「跌得更多」。
 *
 * 同上，四个键全给，否则「阳线空心」的 `transparent` 会漏过来 —— 而一字跌停
 * （开 = 收 = 最低）在 ECharts 眼里正是阳线，漏了就会画成一根空心柱。
 */
const LIMIT_DOWN_ITEM_STYLE = {
  color: KLINE.limitDown,
  color0: KLINE.limitDown,
  borderColor: KLINE.limitDown,
  borderColor0: KLINE.limitDown,
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

/**
 * 不传 `keyLevels` 时用的**稳定空数组**。
 *
 * 不能拿 `keyLevels = []` 当默认参数：那每次调用都会生成一个新数组引用，把下面
 * `useMemo` 的依赖打破 —— 组件每重渲染一次就重算 option、`EChart` 用
 * `setOption(..., true)` 把整张 K 线（蜡烛 + 成交量 + 标注线）重建一遍。
 * 个股详情页没传 keyLevels，于是点「加入自选」之类都会触发整图重建（2026-10-12 修）。
 */
const NO_KEY_LEVELS: KeyLevel[] = []

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
 * K 线图（蜡烛 + 成交量；**不画均线**，见文件顶部那段），个股详情页与形态选股页共用。
 *
 * 2026-09-26 起画法按**同花顺**那套来：阳线空心红 / 阴线实心青、图区比卡片沉一档。
 *
 * ⚠️ **2026-10-15 配色改成 `KLINE`（深底 + 红涨青跌），不再吃站点的浅色 `CHART`**
 * （用户「配色改成图一」）。这是**唯一**一张深底的图：全站仍是浅色（2026-10-13 定的），
 * 另外 8 个图表继续用 `CHART`。两个令牌集的分工与全部采样依据见 `lib/chart.ts` 的
 * `KLINE` 注释。
 *
 * **涨停那天的蜡烛整根实心描金、跌停那天的整根实心绿**（日 K 才有，判据在后端，
 * 见 `LIMIT_UP_ITEM_STYLE` / `LIMIT_DOWN_ITEM_STYLE`）。
 *
 * 从 `StockDetail.tsx` 里抽出来而不是复制一份：这个图有三处容易写错的地方
 * —— A 股的红涨青跌覆盖（ECharts 默认是欧美惯例）、主图与副图的轴联动、
 * 成交量柱跟随涨跌染色 —— 复制出去迟早会有一份忘了改。
 */
export default function KLineChart({ bars, height = 420, keyLevels = NO_KEY_LEVELS }: Props) {
  const option = useMemo<ChartOption>(() => {
    if (bars.length === 0) return {}
    const dates = bars.map((bar) => bar.label)
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
      //
      // ⚠️ 这里用的是 `KLINE.upText` / `downText`（**亮的那档**），**与蜡烛的
      // `KLINE.up` / `down` 不是同一个值**：图一里成交量柱本来就和蜡烛分两个色阶
      // （柱子更亮），本次按用户「改成图一」照抄。本站 2026-09-26 曾把两档统一过一档，
      // 那条统一**在 K 线图上被这次改动推翻**；别的图表不受影响（它们本来只有一档）。
      itemStyle: {
        color:
          (bar.pct_chg ?? (bar.close ?? 0) - (bar.open ?? 0)) >= 0
            ? KLINE.upText
            : KLINE.downText,
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
    // 横轴放几个标签要看标签有多长：周月是 `25-09-30`（8 字符，比日线的 `09-21`
    // 长），同一宽度下要少放几个，否则相邻标签会贴在一起
    const wideLabels = (dates[0] ?? '').length > 6
    const interval = Math.max(0, Math.floor(dates.length / (wideLabels ? 6 : 8)))

    return {
      // 整块画布上深底（**不只两个 grid**）：grid 外面还留着 left 8 / right 14 / 顶 10px /
      // 底部 4% 的边距，只给 grid 上底的话那圈边会露出卡片的白。图一就是整块黑。
      // （旧注释写的是「铺在 grid 上而不是画布整体背景」—— 那是浅色版的做法：图区底只比
      // 卡片白沉一档，整块上底反而会跟卡片圆角打架。深底版正好反过来。）
      backgroundColor: KLINE.page,
      grid: [
        // `top` 只留一点点：图例（均线那几条）已经去掉，把它让给蜡烛
        // 高度 58% → 52%（2026-10-15 用户要求「主图再压缩一点，副图大一点」）
        { left: 8, right: 14, top: 10, height: '52%', containLabel: true, backgroundColor: KLINE.page },
        {
          left: 8,
          right: 14,
          // 成交量副图：起点 62% → 60%、高度 30% → 36%（2026-10-15 第三次按用户要求放大）。
          // 变更史：76%/16% → 67%/25% → 62%/30% → 60%/36%，底边跟着 92% → 96%。
          // **留 4% 是有意的**：再往下副图的横轴线就要贴着卡片的圆角了。下面那 8% 原来是
          // 纯空白（副图横轴 `axisLabel: {show: false}`，`containLabel` 没东西可装）。
          // 主图（grid 0 的 52%）这一次没动。
          top: '60%',
          height: '36%',
          containLabel: true,
          backgroundColor: KLINE.page,
        },
      ],
      tooltip: {
        ...KLINE_TOOLTIP,
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
          axisLabel: { ...KLINE_AXIS_LABEL, interval },
          axisLine: { lineStyle: { color: KLINE.line } },
          axisTick: { show: false },
        },
        {
          type: 'category',
          data: dates,
          gridIndex: 1,
          axisLabel: { show: false },
          axisLine: { lineStyle: { color: KLINE.line } },
          axisTick: { show: false },
        },
      ],
      yAxis: [
        {
          scale: true,
          gridIndex: 0,
          axisLabel: KLINE_AXIS_LABEL,
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
            ...KLINE_AXIS_LABEL,
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
            // 红/青的**具体值**取自图一（`KLINE.up` = `#d90000` 描边、`KLINE.down`
            // = `#17def1` 实体），与成交量柱那两档不是同一个值，见上面 `volumes` 的注释。
            color: 'transparent',
            color0: KLINE.down,
            borderColor: KLINE.up,
            borderColor0: KLINE.down,
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
                  color: KLINE.fgMuted,
                  fontSize: 11,
                  formatter: '{b}',
                },
                data: marks.map((level, index) => ({
                  yAxis: level.value,
                  name: `${level.label} ${level.value.toFixed(2)}`,
                  // 相邻价位分居左右两端（见上面 marks 的注释）
                  label: { position: MARK_LABEL_POSITIONS[index % MARK_LABEL_POSITIONS.length] },
                  lineStyle: {
                    // 「突破」用强调金（深色版旧约定）、「支撑」用次级灰 —— 两条都压
                    // `#10161a` 上，「突破」那条要一眼分得出，所以取金而不是蓝
                    color: level.kind === 'breakout' ? KLINE.accent : KLINE.fgDim,
                    type: 'dashed' as const,
                    width: 1,
                  },
                })),
              }
            : undefined,
        },
        {
          type: 'bar' as const,
          name: '成交量',
          data: volumes,
          xAxisIndex: 1,
          yAxisIndex: 1,
          barMaxWidth: 8,
        },
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
