import type { ReactNode } from 'react'
import type { Sentiment, TurnoverSeries } from '../api/types'
import { breadthRatio, fmtAmount, fmtInt, fmtNum, fmtPct } from '../lib/format'
import Panel from './Panel'

interface SentimentPanelProps {
  sentiment: Sentiment
  /** 截至所选交易日的近 30 日成交额。后端还没给出（老接口缓存）时为 null */
  turnover: TurnoverSeries | null
  delay?: number
}

/** 柱状图的高度（px）。
 *
 * 比一行文字高得多是故意的：成交额的日常振幅本来就只有 1.6~2.6 万亿，
 * 40px 时最高与最矮两根只差 15px，变化看不出来。
 */
const BAR_AREA = 56
/** 左侧 y 轴刻度标签占的宽度（px）。标签是纯数字，单位统一写在下面的小字里 */
const AXIS_WIDTH = 22

/** y 轴刻度的候选步长（单位：万亿）。从密到疏找第一个「不超过 3 档」的 */
const TICK_STEPS = [0.2, 0.5, 1, 2, 5]

/** 万亿为单位，去掉浮点尾巴（0.2 的倍数会算出 0.6000000000000001） */
function fmtWan(value: number): string {
  return String(Number(value.toFixed(2)))
}

/**
 * 把峰值向上取到一个「整」刻度，返回刻度顶与步长（单位：万亿）。
 *
 * **不能拿最大值当顶**：顶值 2.55 会画出 1.27 / 2.55 这种刻度，读的时候得在
 * 脑子里做除法。取整到 3 之后刻度是 1 / 2 / 3，扫一眼就能对上。
 */
function amountTicks(peak: number): { top: number; step: number } {
  const peakWan = peak / 1e12
  const step = TICK_STEPS.find((candidate) => peakWan / candidate <= 3) ?? TICK_STEPS[TICK_STEPS.length - 1]
  return { top: Math.ceil(peakWan / step) * step, step }
}

/**
 * 两市成交额柱状图。
 *
 * **零基线**，不用 min-max 把最小值拉到 0：柱高就是「钱多钱少」的直接读数。
 * 按区间拉伸会把 1.9 与 2.0 万亿的差别也画成两倍高，看着像放量其实没有 ——
 * 而这张图要回答的恰恰是「今天到底放量了没有」。柱高按 y 轴顶（取整后的值）
 * 换算，所以刻度标出来的数字可以直接对着柱子读。
 *
 * 柱子**红绿跟当日上证指数涨跌**（红=涨、绿=跌），不是跟成交额自己比：
 * 这是 K 线副图的老惯例，柱子因此同时说了「量价配合」—— 红柱是放量上涨，
 * 绿柱是缩量下跌。指数涨跌幅缺失或为 0 的日子走中性灰，不误染色。
 *
 * 序列按构造**必然止于所选交易日**，所以最右那根就是当天，不再额外高亮：
 * 红绿已经在承担一层含义，再叠一层明暗会让扫读变慢。
 */
function AmountTrend({
  series,
  current,
}: {
  series: TurnoverSeries | null
  current: number | null
}) {
  const points = series
    ? series.dates.map((date, i) => ({
        date,
        amount: series.amounts[i] ?? null,
        pct: series.pct_chg[i] ?? null,
      }))
    : []
  const values = points
    .map((point) => point.amount)
    .filter((amount): amount is number => amount != null)

  if (points.length === 0 || values.length === 0) {
    return <span className="num text-[11px] text-fg-dim">沪指 + 深成指（无历史序列）</span>
  }

  const peak = Math.max(...values)
  const { top, step } = amountTicks(peak)
  const avg5 = values.slice(-5).reduce((sum, value) => sum + value, 0) / Math.min(5, values.length)
  const today = points[points.length - 1].amount ?? current
  const prev = points.length >= 2 ? points[points.length - 2].amount : null
  const delta =
    today != null && prev != null && prev !== 0 ? (today / prev - 1) * 100 : null

  // 刻度从 1 倍步长开始（0 就是那根基线，不用再标一个「0」）
  const ticks: number[] = []
  for (let i = 1; i * step <= top + 1e-9; i += 1) ticks.push(i * step)

  // ⚠️ `amount` 是**元**，而 `top` 是**万亿** —— 必须先把轴顶换算成元再作比，
  // 否则柱高会算成 1e13 px 量级，被浏览器钳到布局上限，柱子糊满整个面板
  const axisTopYuan = top * 1e12

  return (
    <div>
      {/* pt-1.5：最上面那个刻度标签是骑在网格线上的，一半字高会探出轴容器，
          不给这点余量就会被下面的数字挤住（实测只剩 3px） */}
      <div className="flex gap-1.5 pt-1.5">
        <div className="relative shrink-0" style={{ width: AXIS_WIDTH, height: BAR_AREA }}>
          {ticks.map((value) => (
            <span
              key={value}
              className="num absolute right-0 text-[10px] leading-none text-fg-dim"
              style={{ bottom: `${(value / top) * BAR_AREA}px`, transform: 'translateY(50%)' }}
            >
              {fmtWan(value)}
            </span>
          ))}
        </div>

        <div className="relative flex flex-1 items-end gap-px" style={{ height: BAR_AREA }}>
          {/* 零基线画成元素，而不是给容器加 border-b：border 会把内容盒底部
              抬高 1px，而左边的轴容器没有边框，两边的 0 基线就错开、刻度对不上网格线 */}
          <div
            className="pointer-events-none absolute right-0 bottom-0 left-0"
            style={{ height: 1, backgroundColor: 'var(--color-line)' }}
          />
          {/* 网格线在柱子之前渲染，靠 z 序压在柱底；挡不到柱子，也不吃鼠标事件 */}
          {ticks.map((value) => (
            <div
              key={value}
              className="pointer-events-none absolute right-0 left-0"
              style={{
                bottom: `${(value / top) * BAR_AREA}px`,
                height: 1,
                backgroundColor: 'rgb(255 255 255 / 0.06)',
              }}
            />
          ))}
          {points.map((point) => {
            const height =
              point.amount == null
                ? 0
                : Math.max(2, (point.amount / axisTopYuan) * BAR_AREA)
            const tone =
              point.pct == null || point.pct === 0
                ? 'bg-fg-dim/60'
                : point.pct > 0
                  ? 'bg-up'
                  : 'bg-down'
            return (
              <div
                key={point.date}
                title={`${point.date}  ${fmtAmount(point.amount)}  沪指 ${fmtPct(point.pct)}`}
                className={`flex-1 ${tone}`}
                style={{ height: `${height}px` }}
              />
            )
          })}
        </div>
      </div>

      <div className="num mt-1.5 flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-[11px] text-fg-dim">
        <span>
          较昨日 <span className="text-fg-muted">{fmtPct(delta)}</span>
        </span>
        <span>
          5 日均 <span className="text-fg-muted">{fmtAmount(avg5)}</span>
        </span>
        {/* 零基线没有 y 轴的绝对值，刻度是纯数字，单位在这里说一次 */}
        <span>单位 万亿</span>
        <span>柱色跟沪指涨跌</span>
      </div>
    </div>
  )
}

interface MetricProps {
  label: string
  value: string
  tone?: string
  sub?: ReactNode
  span?: string
  /** 把明细推到这个格子的底部。见下面 `Metric` 里的说明 */
  fill?: boolean
}

function Metric({
  label,
  value,
  tone = 'text-fg',
  sub,
  span = '',
  fill = false,
}: MetricProps) {
  return (
    <div
      className={`relative -mr-px -mb-px border-r border-b border-line-soft px-4 py-3 ${span} ${
        fill ? 'flex flex-col' : ''
      }`}
    >
      <div className="text-[11px] tracking-[0.1em] text-fg-dim">{label}</div>
      <div className={`num mt-1.5 text-[26px] leading-none font-medium ${tone}`}>
        {value}
      </div>
      {/* fill 用于同一行里装着柱状图的邻居格：Grid 会把整行拉齐，
          内容少的那格若不给推开，底部会空出一大块，看着像没渲染完 */}
      {sub && <div className={fill ? 'mt-auto pt-2' : 'mt-2'}>{sub}</div>}
    </div>
  )
}

/** 细色条：把比率编码进长度，比再写一个百分数更快扫到。 */
function Meter({ ratio, from, to }: { ratio: number; from: string; to: string }) {
  return (
    <div className="flex h-[3px] w-full overflow-hidden bg-ink-700">
      <div
        className="wipe h-full"
        style={{ width: `${ratio * 100}%`, backgroundColor: from }}
      />
      <div className="h-full flex-1" style={{ backgroundColor: to }} />
    </div>
  )
}

/**
 * 市场情绪面板。
 *
 * 涨停/跌停/炸板家数一律以 akshare 涨停池为准 —— iFinD 的涨跌家数只对上证指数
 * 有效，深市返回 null，深证成指又只含 500 只成分股，会严重低估。
 * 涨跌家数取乐咕乐股的全市宽度，涨跌超 5% 取 iFinD 选股的 matched（同为全市场口径，
 * **不能**拿本地池子去数：池子只有成交额 1 亿以上的 3032 只，涨停股实测 37% 在池外）。
 *
 * 四行分层：涨停板本身 → 率与波动强度 → 宽度与量能 → 接力溢价。
 * 每行各自成组，扫读时不用在十几个数字里找关联。
 */
export default function SentimentPanel({
  sentiment: s,
  turnover,
  delay = 120,
}: SentimentPanelProps) {
  const limitTotal = (s.limit_up_count ?? 0) + (s.broken_count ?? 0)
  const sealRatio = limitTotal > 0 ? (s.limit_up_count ?? 0) / limitTotal : 0
  // 涨跌超 5% 与涨跌家数一样只有当日值：历史日期留空，如实说明而不是填 0
  const hasFive = s.up5_count != null || s.down5_count != null

  return (
    <Panel
      title="市场情绪"
      meta={<span className="num">{s.trade_date} 收盘</span>}
      delay={delay}
    >
      <div className="grid grid-cols-2 overflow-hidden md:grid-cols-4">
        <Metric
          label="涨停"
          value={fmtInt(s.limit_up_count)}
          tone="text-up"
          sub={<span className="num text-[11px] text-fg-dim">含首板与连板</span>}
        />
        <Metric
          label="跌停"
          value={fmtInt(s.limit_down_count)}
          tone="text-down"
          sub={<span className="num text-[11px] text-fg-dim">当日跌停家数</span>}
        />
        <Metric
          label="炸板"
          value={fmtInt(s.broken_count)}
          tone="text-fg-muted"
          sub={<span className="num text-[11px] text-fg-dim">触板未封住</span>}
        />
        <Metric
          label="最高连板"
          value={fmtInt(s.max_consecutive)}
          tone="text-accent"
          sub={<span className="num text-[11px] text-fg-dim">市场高度</span>}
        />

        <Metric
          label="封板率"
          value={fmtNum(s.seal_rate, 2, '%')}
          tone="text-up"
          sub={<Meter ratio={sealRatio} from="var(--color-up)" to="var(--color-ink-700)" />}
        />
        <Metric
          label="炸板率"
          value={fmtNum(s.broken_rate, 2, '%')}
          tone="text-fg-muted"
          sub={
            <Meter
              ratio={1 - sealRatio}
              from="var(--color-fg-dim)"
              to="var(--color-ink-700)"
            />
          }
        />
        <Metric
          label="涨超 5%"
          value={fmtInt(s.up5_count)}
          tone={s.up5_count == null ? 'text-fg-dim' : 'text-up'}
          sub={
            <span className="num text-[11px] text-fg-dim">
              {hasFive ? '全市场，含涨停' : '仅当日可取'}
            </span>
          }
        />
        <Metric
          label="跌超 5%"
          value={fmtInt(s.down5_count)}
          tone={s.down5_count == null ? 'text-fg-dim' : 'text-down'}
          sub={
            <span className="num text-[11px] text-fg-dim">
              {hasFive ? '全市场，含跌停' : '仅当日可取'}
            </span>
          }
        />

        <Metric
          span="col-span-2 md:col-span-2"
          fill
          label="涨 / 跌家数"
          value={`${fmtInt(s.up_count)} / ${fmtInt(s.down_count)}`}
          sub={
            s.up_count != null && s.down_count != null ? (
              <div className="space-y-1.5">
                <Meter
                  ratio={breadthRatio(s.up_count, s.down_count)}
                  from="var(--color-up)"
                  to="var(--color-down)"
                />
                <div className="num flex justify-between text-[11px] text-fg-dim">
                  <span className="text-up">涨 {s.up_count}</span>
                  <span className="text-down">跌 {s.down_count}</span>
                </div>
              </div>
            ) : (
              <span className="num text-[11px] text-fg-dim">历史数据源仅提供当日值</span>
            )
          }
        />
        <Metric
          span="col-span-2 md:col-span-2"
          label="两市成交额"
          value={fmtAmount(s.total_amount)}
          sub={<AmountTrend series={turnover} current={s.total_amount} />}
        />

        <Metric
          span="col-span-2 md:col-span-4"
          label="昨日涨停溢价"
          value={fmtPct(s.yesterday_limit_today_avg)}
          tone={
            s.yesterday_limit_today_avg == null
              ? 'text-fg-dim'
              : s.yesterday_limit_today_avg > 0
                ? 'text-up'
                : 'text-down'
          }
          sub={
            <span className="text-[11px] leading-relaxed text-fg-dim">
              昨日涨停股今日的均涨幅 ——
              <span className="text-fg-muted">短线接力情绪的体温计</span>：
              为正说明昨天的板今天有人接、追高的容错率高；为负说明市场在杀接力，
              当天再追涨停要谨慎。（历史日期没有这项：它要当日实时涨幅才能算）
            </span>
          }
        />
      </div>
    </Panel>
  )
}
