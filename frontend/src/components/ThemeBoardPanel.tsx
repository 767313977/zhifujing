import { useEffect, useMemo, useState } from 'react'
import { api, isNoDataError } from '../api/client'
import type { ThemeBoard } from '../api/types'
import LoadState from './LoadState'
import Panel from './Panel'
import StockLink from './StockLink'
import { fmtAmount, fmtPct, toneOf } from '../lib/format'

/**
 * 今日主线题材看板（FEAT-THEME-BOARD，2026-10-31）。
 *
 * 数据全部来自 `GET /api/themes/board`（纯聚合本地涨停池 + 精选板块 + 板块日线）。
 * 「主线」= 同一精选板块下 ≥3 只涨停股；家数与 `/sectors`、`/limit-up` 同源，
 * 一只票可挂多个题材，所以各行之和会大于当日涨停总数 —— 表尾写明这一点。
 *
 * 龙头 / 中军 / 跟风是**确定性规则**（见后端 `services.theme_board`），不是荐股：
 * 龙头=连板最高，中军=剩余里总市值最大，跟风=其余按连板/封单取前 2。
 */

const DEFAULT_TOP = 5

const STATE_CLASS: Record<string, string> = {
  新晋: 'text-accent',
  延续: 'text-fg-muted',
  退潮: 'text-fg-dim',
}

export default function ThemeBoardPanel({
  date,
  onSelectConcept,
}: {
  date: string | null
  /** 点题材名时回调（板块页用它切到该板块）；不传就只显示文字 */
  onSelectConcept?: (concept: string) => void
}) {
  const [data, setData] = useState<ThemeBoard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)

  useEffect(() => {
    let stale = false
    setLoading(true)
    setError(null)
    api
      .themeBoard(date)
      .then((value) => {
        if (!stale) setData(value)
      })
      .catch((err: unknown) => {
        if (stale) return
        setData(null)
        setError(isNoDataError(err) ? null : (err as Error).message)
      })
      .finally(() => {
        if (!stale) setLoading(false)
      })
    return () => {
      stale = true
    }
  }, [date])

  const rows = useMemo(() => {
    if (!data) return []
    return showAll ? data.rows : data.rows.slice(0, DEFAULT_TOP)
  }, [data, showAll])

  return (
    <div className="space-y-4">
      <Panel
        title="今日主线"
        meta={
          data ? (
            <span className="num">
              {data.trade_date}
              {data.prev_trade_date ? ` · 对比 ${data.prev_trade_date}` : ''} · 涨停{' '}
              {data.limit_up_total} 只
              {data.etf_date ? ` · ETF 份额 ${data.etf_date}` : ''}
            </span>
          ) : null
        }
        delay={20}
      >
        {data === null ? (
          <LoadState loading={loading} error={error} emptyText="暂无主线数据" />
        ) : data.rows.length === 0 ? (
          <LoadState emptyText={data.note ?? '当日没有可识别的主线'} />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th>名次</th>
                    <th className="text-left">题材</th>
                    <th>状态</th>
                    <th>涨停数</th>
                    <th>最高板</th>
                    <th>涨幅</th>
                    <th>净流入</th>
                    <th className="text-left">相关ETF</th>
                    <th className="text-left">龙头 / 中军 / 跟风</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.concept}>
                      <td className="num">
                        {row.rank}
                        {row.rank_change != null && row.rank_change !== 0 && (
                          <span
                            className={row.rank_change > 0 ? 'ml-1 text-up' : 'ml-1 text-down'}
                            title={`昨日第 ${row.prev_rank} 名`}
                          >
                            {row.rank_change > 0 ? '↑' : '↓'}
                            {Math.abs(row.rank_change)}
                          </span>
                        )}
                        {row.state === '新晋' && <span className="ml-1 text-accent">新</span>}
                      </td>
                      <td className="!text-left">
                        {onSelectConcept ? (
                          <button
                            type="button"
                            onClick={() => onSelectConcept(row.concept)}
                            className="text-left text-fg transition-colors hover:text-accent"
                            title="切到板块页看这个题材"
                          >
                            {row.concept}
                          </button>
                        ) : (
                          <span className="text-fg">{row.concept}</span>
                        )}
                      </td>
                      <td className={`text-[12px] ${STATE_CLASS[row.state] ?? ''}`}>
                        {row.state}
                      </td>
                      <td className="num">{row.limit_up_count}</td>
                      <td className="num">{row.max_consecutive ?? '—'}</td>
                      <td className={`num ${toneOf(row.pct_chg)}`}>{fmtPct(row.pct_chg)}</td>
                      <td className="num text-fg-muted">{fmtAmount(row.net_inflow)}</td>
                      <td className="!text-left">
                        {row.etfs.length === 0 ? (
                          <span className="text-fg-dim">—</span>
                        ) : (
                          <div className="flex flex-wrap gap-x-2 gap-y-0.5">
                            {row.etfs.map((etf) => (
                              <span
                                key={etf.code}
                                className="whitespace-nowrap text-[12px] text-fg-muted"
                                title={`${etf.code} 当日净申赎`}
                              >
                                {etf.name ?? etf.code}
                                <span
                                  className={`num ml-1 ${
                                    etf.net > 0 ? 'text-up' : etf.net < 0 ? 'text-down' : ''
                                  }`}
                                >
                                  {fmtAmount(etf.net)}
                                </span>
                              </span>
                            ))}
                          </div>
                        )}
                      </td>
                      <td className="!text-left">
                        <div className="flex flex-wrap gap-x-2 gap-y-1">
                          {row.leaders.map((item) => (
                            <span
                              key={item.code}
                              className="whitespace-nowrap text-[12px] text-fg-dim"
                              title={`${item.role}${item.consecutive ? ` · ${item.consecutive} 板` : ''}`}
                            >
                              <span className={item.role === '龙头' ? 'text-accent' : ''}>
                                {item.role}
                              </span>
                              <StockLink code={item.code} className="ml-1 text-fg-muted">
                                {item.name ?? item.code}
                              </StockLink>
                            </span>
                          ))}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {data.rows.length > DEFAULT_TOP && (
              <div className="border-t border-line-soft px-4 py-2">
                <button
                  type="button"
                  onClick={() => setShowAll((prev) => !prev)}
                  className="text-[12px] text-accent hover:underline"
                >
                  {showAll ? '只看前 5 名' : `展开其余 ${data.rows.length - DEFAULT_TOP} 个题材`}
                </button>
              </div>
            )}
          </>
        )}
        {data && data.fading.length > 0 && (
          <div className="border-t border-line-soft px-4 py-2 text-[12px] text-fg-muted">
            <span className="text-fg-dim">退潮：</span>
            {data.fading.map((row, index) => (
              <span key={row.concept} className="mr-3 whitespace-nowrap">
                {index > 0 && <span className="mr-3 text-line">|</span>}
                {row.concept}
                <span className="text-fg-dim">
                  （昨 {row.prev_count} 家
                  {row.prev_rank != null ? ` · 第 ${row.prev_rank} 名` : ''}）
                </span>
              </span>
            ))}
          </div>
        )}
        {data && data.rows.length > 0 && (
          <p className="px-4 py-3 text-[12px] leading-relaxed text-fg-dim">
            主线的「涨停数」按**开盘红精选板块**归属统计，与板块页、涨停复盘同源；
            一只票可以挂多个题材，所以各行之和会大于当日涨停总数（{data.limit_up_total} 只）。
            龙头 / 中军 / 跟风是规则排出来的，不是荐股。
            「相关ETF」是**手工映射**（每个方向一两只代表 ETF），净申赎只在该 ETF
            有份额快照时才显示，没数据就是「—」。
          </p>
        )}
      </Panel>
    </div>
  )
}
