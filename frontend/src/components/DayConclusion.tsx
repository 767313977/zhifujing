import type { MarketOverview } from '../api/types'
import { fmtAmount, fmtInt, fmtNum, fmtPct, toneOf } from '../lib/format'
import ConclusionBar, { ConclusionStat } from './ConclusionBar'

/**
 * 今日结论条（规范 v1 §5，Phase 4，2026-11-01）。
 *
 * **只重排已有字段，不引入任何新口径、不请求新接口**：
 * - 左侧结论 = 情绪表里的四个现成数（涨停家数、最高连板、封板率、昨日涨停今日均涨），
 *   按固定模板拼成一句话；哪个字段为空就跳过哪一段，全空时明说「数据未入库」。
 * - 中间一行 = 三大指数的涨跌幅（上证 / 深证 / 创业板，取不到的跳过）。
 * - 右侧 = 涨跌家数、成交额、炸板数、恐贪指数 —— 都是 `overview` 里已经返回的值。
 *
 * ⚠️ 这里**不写「普涨 / 分化 / 退潮」这类判断词**：那类判读由后端
 * `overview.divergence`（判读条）负责，前端再自造一套会与它打架。
 */

/** 三大指数：结论条只点名这三个，其余指数在下面的指数条里 */
const BIG_THREE = ['000001.SH', '399001.SZ', '399006.SZ']

export default function DayConclusion({ overview }: { overview: MarketOverview }) {
  const sentiment = overview.sentiment
  const fearGreed = overview.fear_greed
  // level 是稳定 key（greed / fear…），中文名要从五档阈值里查 —— 后端只保证 key 稳定
  const fearBand = fearGreed?.bands.find((band) => band.key === fearGreed.level)
  const fearText =
    fearGreed?.score != null
      ? `${Math.round(fearGreed.score)} ${fearBand?.label ?? fearGreed.level ?? ''}`.trim()
      : null
  const bigThree = BIG_THREE.map((code) =>
    overview.indexes.find((item) => item.code === code),
  ).filter((item): item is NonNullable<typeof item> => item != null)

  const parts: string[] = []
  if (sentiment?.limit_up_count != null) parts.push(`涨停 ${sentiment.limit_up_count} 家`)
  if (sentiment?.max_consecutive != null) {
    parts.push(`最高 ${sentiment.max_consecutive} 板`)
  }
  if (sentiment?.seal_rate != null) {
    parts.push(`封板率 ${fmtNum(sentiment.seal_rate, 2, '%')}`)
  }
  if (sentiment?.yesterday_limit_today_avg != null) {
    parts.push(`昨涨停今均涨 ${fmtPct(sentiment.yesterday_limit_today_avg)}`)
  }
  const headline = parts.length > 0 ? parts.join(' · ') : '当日情绪数据未入库'

  const breadth =
    sentiment?.up_count != null || sentiment?.down_count != null
      ? `${fmtInt(sentiment?.up_count)} / ${fmtInt(sentiment?.down_count)}`
      : '—'

  return (
    <ConclusionBar
      headline={headline}
      sub={
        bigThree.length > 0 ? (
          bigThree.map((item) => (
            <span key={item.code}>
              {item.name ?? item.code}
              <span className={`ml-1 ${toneOf(item.pct_chg)}`}>{fmtPct(item.pct_chg)}</span>
            </span>
          ))
        ) : (
          <span>三大指数暂无涨跌幅</span>
        )
      }
      stats={
        <>
          <ConclusionStat
            label="涨/跌"
            value={breadth}
            title="全市场上涨 / 下跌家数（数据源只给当日）"
          />
          <ConclusionStat label="成交额" value={fmtAmount(sentiment?.total_amount)} />
          <ConclusionStat label="炸板" value={fmtInt(sentiment?.broken_count)} />
          {fearText ? (
            <ConclusionStat
              label="恐贪"
              value={fearText}
              title="恐惧贪婪指数（0–100，越高越贪婪）"
            />
          ) : (
            <ConclusionStat
              label="恐贪"
              value="—"
              title="当日恐贪指数不可用（历史分位不足或未入库）"
            />
          )}
        </>
      }
    />
  )
}
