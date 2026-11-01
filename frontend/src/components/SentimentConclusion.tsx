import type { Breadth, FearGreed, RelayTemp, Sentiment } from '../api/types'
import { fmtInt, fmtNum, fmtPct, toneOf } from '../lib/format'
import ConclusionBar, { ConclusionStat } from './ConclusionBar'

/**
 * 情绪周期结论条（规范 v1 §5，Phase 6，2026-11-01）。
 *
 * 全部来自本页已经加载的四个序列的**最新一个点**，不新算任何分位 / 信号：
 * - 涨停 / 最高连板 / 封板率 / 跌停 / 炸板 / 昨涨停今均涨 ← `sentiment`
 * - 恐贪分档、接力温度分档 ← 两个序列自带的 `latest` + 后端给的 `bands`（按 key 查中文名）
 * - 宽度 MA20 ← `breadth.latest`
 *
 * **缺值一律 `—`**：分位不足、当日未采集、接口 404 时这些字段就是 null，
 * 不拿 0 顶（0 在恐贪里是「极度恐慌」，含义完全相反）。
 */
export default function SentimentConclusion({
  sentiment,
  fearGreed,
  relayTemp,
  breadth,
  loading = false,
}: {
  sentiment: Sentiment[]
  fearGreed: FearGreed | null
  relayTemp: RelayTemp | null
  breadth: Breadth | null
  loading?: boolean
}) {
  const latest = sentiment.length > 0 ? sentiment[sentiment.length - 1] : null

  // 分档中文名从后端给的 bands 里按 key 查，前端不写死 25/45/55/75
  const fearLevel = fearGreed?.latest?.level
    ? (fearGreed.bands.find((band) => band.key === fearGreed.latest?.level)?.label ??
      fearGreed.latest.level)
    : null
  const fearText =
    fearGreed?.latest?.score != null
      ? `${fmtNum(fearGreed.latest.score, 0)}${fearLevel ? ` ${fearLevel}` : ''}`
      : '—'

  const relayLevel = relayTemp?.latest?.level
    ? (relayTemp.bands.find((band) => band.key === relayTemp.latest?.level)?.label ??
      relayTemp.latest.level)
    : null
  const relayText =
    relayTemp?.latest?.score != null
      ? `${fmtNum(relayTemp.latest.score, 0)}${relayLevel ? ` ${relayLevel}` : ''}`
      : '—'

  const parts: string[] = []
  if (latest?.limit_up_count != null) parts.push(`涨停 ${latest.limit_up_count} 家`)
  if (latest?.max_consecutive != null) parts.push(`最高 ${latest.max_consecutive} 板`)
  if (latest?.seal_rate != null) parts.push(`封板率 ${fmtNum(latest.seal_rate, 2, '%')}`)
  const headline = loading
    ? '加载中…'
    : parts.length > 0
      ? parts.join(' · ')
      : '情绪数据未入库'

  return (
    <ConclusionBar
      headline={headline}
      sub={
        <>
          <span>
            恐贪 <span className="text-fg-muted">{fearText}</span>
          </span>
          <span>
            接力温度 <span className="text-fg-muted">{relayText}</span>
          </span>
          <span>
            宽度 MA20{' '}
            <span className="text-fg-muted">
              {breadth?.latest?.above_ma20 != null
                ? fmtNum(breadth.latest.above_ma20, 1, '%')
                : '—'}
            </span>
          </span>
        </>
      }
      stats={
        <>
          <ConclusionStat
            label="跌停"
            value={fmtInt(latest?.limit_down_count)}
            title="当日跌停家数（涨停板三池口径）"
          />
          <ConclusionStat
            label="炸板"
            value={fmtInt(latest?.broken_count)}
            title="当日炸板家数"
          />
          <ConclusionStat
            label="昨涨停今均涨"
            value={fmtPct(latest?.yesterday_limit_today_avg)}
            tone={toneOf(latest?.yesterday_limit_today_avg)}
            title="昨日涨停股今日平均涨跌幅"
          />
          <ConclusionStat
            label="恐贪"
            value={fearText}
            title="恐惧贪婪指数（0–100，越高越贪婪）；分位历史不足时显示 —"
          />
        </>
      }
    />
  )
}
