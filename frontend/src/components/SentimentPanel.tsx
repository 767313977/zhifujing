import type { ReactNode } from 'react'
import type { Sentiment } from '../api/types'
import { breadthRatio, fmtAmount, fmtInt, fmtNum, fmtPct } from '../lib/format'
import Panel from './Panel'

interface SentimentPanelProps {
  sentiment: Sentiment
  delay?: number
}

interface MetricProps {
  label: string
  value: string
  tone?: string
  sub?: ReactNode
  span?: string
}

function Metric({ label, value, tone = 'text-fg', sub, span = '' }: MetricProps) {
  return (
    <div
      className={`relative -mr-px -mb-px border-r border-b border-line-soft px-4 py-3 ${span}`}
    >
      <div className="text-[11px] tracking-[0.1em] text-fg-dim">{label}</div>
      <div className={`num mt-1.5 text-[26px] leading-none font-medium ${tone}`}>
        {value}
      </div>
      {sub && <div className="mt-2">{sub}</div>}
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
 * 涨跌家数取乐咕乐股的全市宽度。
 */
export default function SentimentPanel({ sentiment: s, delay = 120 }: SentimentPanelProps) {
  const limitTotal = (s.limit_up_count ?? 0) + (s.broken_count ?? 0)
  const sealRatio = limitTotal > 0 ? (s.limit_up_count ?? 0) / limitTotal : 0

  return (
    <Panel
      title="市场情绪"
      meta={<span className="num">{s.trade_date} 收盘</span>}
      delay={delay}
    >
      <div className="grid grid-cols-3 overflow-hidden md:grid-cols-6">
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
          label="最高连板"
          value={fmtInt(s.max_consecutive)}
          tone="text-accent"
          sub={<span className="num text-[11px] text-fg-dim">市场高度</span>}
        />

        <Metric
          span="col-span-3 md:col-span-2"
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
          span="col-span-3 md:col-span-2"
          label="两市成交额"
          value={fmtAmount(s.total_amount)}
          sub={<span className="num text-[11px] text-fg-dim">上证指数 + 深证成指</span>}
        />
        <Metric
          span="col-span-3 md:col-span-2"
          label="打板效应"
          value={fmtPct(s.yesterday_limit_today_avg)}
          tone={
            s.yesterday_limit_today_avg == null
              ? 'text-fg-dim'
              : s.yesterday_limit_today_avg > 0
                ? 'text-up'
                : 'text-down'
          }
          sub={
            <span className="num text-[11px] text-fg-dim">
              昨日涨停股今日均涨幅，衡量接力赚钱效应
            </span>
          }
        />
      </div>
    </Panel>
  )
}
