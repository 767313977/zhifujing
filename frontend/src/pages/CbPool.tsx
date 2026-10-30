import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { CbExcludedRow, CbPool, CbPoolRow } from '../api/types'
import Layout from '../components/Layout'
import LoadState from '../components/LoadState'
import Panel from '../components/Panel'
import SortTh from '../components/SortTh'
import StockLink from '../components/StockLink'
import { fmtAmount, fmtNum, fmtPct, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'

/**
 * 可转债 T+0 观察池。
 *
 * 这是**盘后候选表**，不是买卖指令：每天收盘后（17:30 那趟）采一次快照，按
 * 「先过硬门槛、再按五因子打分」排出第二天的观察顺序。转债 T+0 的亏损大头是踩雷
 * （强赎 / 小盘 / 高溢价），所以页面把「被剔除的原因」也完整列出来 —— 少一只比
 * 多一只重要。
 *
 * 打分口径见后端 `services/cb_pool`，页面上每个数字都写清了口径，不猜。
 */

/** 观察池表的排序口径。首屏按评分（后端也是这个顺序）。 */
const POOL_SORTS: SortSpecs<CbPoolRow> = {
  score: { value: (row) => row.score },
  name: { value: (row) => row.name ?? row.code, first: 'asc' },
  close: { value: (row) => row.close },
  pct_chg: { value: (row) => row.pct_chg },
  amplitude: { value: (row) => row.amplitude },
  amount: { value: (row) => row.amount },
  turnover_rate: { value: (row) => row.turnover_rate },
  premium_rt: { value: (row) => row.premium_rt, first: 'asc' },
  double_low: { value: (row) => row.double_low, first: 'asc' },
  remain_size: { value: (row) => row.remain_size },
  rating: { value: (row) => row.rating, first: 'asc' },
  stock_pct_chg: { value: (row) => row.stock_pct_chg },
  redeem_status: { value: (row) => row.redeem_status, first: 'asc' },
}

/** 未通过门槛表的排序口径。 */
const EXCLUDED_SORTS: SortSpecs<CbExcludedRow> = {
  name: { value: (row) => row.name ?? row.code, first: 'asc' },
  close: { value: (row) => row.close },
  amount: { value: (row) => row.amount },
  premium_rt: { value: (row) => row.premium_rt, first: 'asc' },
  reason: { value: (row) => row.reason, first: 'asc' },
}

const NO_ROWS: CbPoolRow[] = []
const NO_EXCLUDED: CbExcludedRow[] = []

/** 评分色阶：只有够高的才染色，中间一律中性（避免满屏高亮）。 */
function scoreTone(score: number): string {
  if (score >= 75) return 'text-accent'
  if (score >= 60) return 'text-fg'
  return 'text-fg-muted'
}

export default function CbPool() {
  const [data, setData] = useState<CbPool | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showExcluded, setShowExcluded] = useState(false)

  useEffect(() => {
    let stale = false
    api
      .cbPool()
      .then((value) => {
        if (!stale) setData(value)
      })
      .catch((err: Error) => {
        if (!stale) setError(err.message)
      })
    return () => {
      stale = true
    }
  }, [])

  // 排序在组件顶层无条件调用（hooks 规则）；数据没到时传空数组常量
  const [sort, rows] = useSort(data?.rows ?? NO_ROWS, POOL_SORTS, { key: 'score' })
  const [excludedSort, excludedRows] = useSort(
    data?.excluded ?? NO_EXCLUDED,
    EXCLUDED_SORTS,
    { key: null },
  )

  const asOf = data?.trade_date ?? null

  return (
    <Layout
      toolbar={
        <span className="num hidden text-[13px] text-fg-dim lg:inline">
          {asOf ? `${asOf} · 收盘快照` : '加载中…'}
        </span>
      }
    >
      <div className="space-y-4">
        <Panel
          title="可转债 T+0 观察池"
          meta={
            <span className="num">
              {data === null
                ? '—'
                : `快照 ${data.total} 只 · 通过门槛 ${data.rows.length} 只`}
            </span>
          }
          delay={40}
        >
          <p className="border-b border-line-soft px-4 py-2 text-[12px] leading-relaxed text-fg-dim">
            先过五道硬门槛（强赎 / 价格 100~150 / 溢价 ≤30% / 成交额 ≥5000 万 /
            剩余规模 ≥3 亿、剩余期限 &gt;1 年、评级 ≥AA-），再按
            <span className="text-fg-muted"> 正股涨幅 30% + 换手 25% + 低溢价 20% +
            振幅 15% + 小盘 10% </span>
            打分。分位是<span className="text-fg-muted">当天池内</span>的相对位置，
            不是历史分位 —— 转债没有足够长的本地历史，用历史分位是假精确。
          </p>

          {data !== null && data.gates.length > 0 && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line-soft px-4 py-2 text-[12px] text-fg-dim">
              <span>已剔除：</span>
              {data.gates.map((gate) => (
                <span key={gate.label} className="num">
                  {gate.label}
                  <span className="ml-1 text-fg-muted">{gate.count}</span>
                </span>
              ))}
            </div>
          )}

          {error ? (
            <LoadState loading={false} error={error} />
          ) : data === null ? (
            <LoadState loading emptyText="加载中" />
          ) : data.rows.length === 0 ? (
            <LoadState
              loading={false}
              emptyText={
                data.total === 0
                  ? '还没有可转债数据 —— 收盘后 17:30 那一趟才会采（当天采不到就是空）'
                  : '今天没有一只通过门槛 —— 说明全市场都在雷区里，空仓也是一种结论'
              }
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <SortTh
                      sortKey="score"
                      title="正股涨幅 30% + 换手 25% + 低溢价 20% + 振幅 15% + 小盘 10%"
                      {...sort}
                    >
                      评分
                    </SortTh>
                    <SortTh sortKey="name" align="left" {...sort}>
                      转债
                    </SortTh>
                    <SortTh sortKey="close" {...sort}>
                      现价
                    </SortTh>
                    <SortTh sortKey="pct_chg" {...sort}>
                      涨幅
                    </SortTh>
                    <SortTh sortKey="amplitude" title="当日振幅：(最高−最低)/昨收" {...sort}>
                      振幅
                    </SortTh>
                    <SortTh sortKey="amount" {...sort}>
                      成交额
                    </SortTh>
                    <SortTh sortKey="turnover_rate" title="成交额 ÷ 剩余规模" {...sort}>
                      换手
                    </SortTh>
                    <SortTh sortKey="premium_rt" title="转股溢价率，越低跟涨越紧" {...sort}>
                      溢价
                    </SortTh>
                    <SortTh
                      sortKey="double_low"
                      title="双低 = 现价 + 转股溢价率，越低越便宜"
                      {...sort}
                    >
                      双低
                    </SortTh>
                    <SortTh sortKey="remain_size" title="剩余规模（亿），集思录没记录时不显示" {...sort}>
                      剩余
                    </SortTh>
                    <SortTh sortKey="rating" {...sort}>
                      评级
                    </SortTh>
                    <SortTh sortKey="stock_pct_chg" align="left" {...sort}>
                      正股
                    </SortTh>
                    <SortTh sortKey="redeem_status" {...sort}>
                      强赎
                    </SortTh>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.code}>
                      <td>
                        <span className={`num ${scoreTone(row.score)}`}>
                          {fmtNum(row.score, 1)}
                        </span>
                      </td>
                      <td className="!text-left">
                        <span className="text-fg">{row.name ?? row.code}</span>
                        <span className="num ml-2 text-[12px] text-fg-dim">{row.code}</span>
                      </td>
                      <td className="num">{fmtNum(row.close, 2)}</td>
                      <td className={`num ${toneOf(row.pct_chg)}`}>{fmtPct(row.pct_chg)}</td>
                      <td className="num text-fg-muted">{fmtPct(row.amplitude, 1)}</td>
                      <td className="num text-fg-muted">{fmtAmount(row.amount)}</td>
                      <td className="num text-fg-muted">{fmtPct(row.turnover_rate, 1)}</td>
                      <td className="num">{fmtPct(row.premium_rt, 1)}</td>
                      <td className="num text-fg-muted">{fmtNum(row.double_low, 1)}</td>
                      <td className="num">
                        {row.remain_size == null ? '—' : `${fmtNum(row.remain_size, 1)} 亿`}
                      </td>
                      <td className="num text-fg-muted">{row.rating ?? '—'}</td>
                      <td className="!text-left">
                        {row.stock_code ? (
                          <>
                            <StockLink
                              code={row.stock_code}
                              className={`text-[13px] ${toneOf(row.stock_pct_chg)}`}
                            >
                              {row.stock_name ?? row.stock_code}
                            </StockLink>
                            <span className={`num ml-1 ${toneOf(row.stock_pct_chg)}`}>
                              {fmtPct(row.stock_pct_chg)}
                            </span>
                          </>
                        ) : (
                          <span className="text-fg-dim">—</span>
                        )}
                      </td>
                      <td className="!text-left text-[12px] text-fg-muted">
                        {row.redeem_status ?? '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="px-4 py-3 text-[12px] leading-relaxed text-fg-dim">
            盘后候选，不是买卖建议。转债 T+0 的实际执行更看盘中：只做 9:30–10:00 与
            14:00–14:57 两段，买卖价差 &gt;0.3% 的不做，单笔止损 1%~1.5%、
            <span className="text-fg-muted">一天亏两笔就收手</span>，不过夜。
            数据来自新浪行情 + 东财条款 + 集思录强赎，**只能当天采、历史不可回补**。
          </p>
        </Panel>

        <Panel
          title="未通过门槛"
          meta={
            <span className="num">
              {data === null ? '—' : `${data.excluded.length} 只`}
            </span>
          }
          delay={70}
        >
          <div className="px-4 py-2 text-[12px] text-fg-dim">
            <button
              type="button"
              onClick={() => setShowExcluded((open) => !open)}
              className="w-full text-left leading-relaxed hover:text-fg"
            >
              这些是今天被硬门槛剔掉的转债 —— 看它们是为了知道「钱在往哪儿去」，
              别去接已经公告强赎的那几只。
              <span className="ml-2 text-fg-muted">{showExcluded ? '收起 ▴' : '展开明细 ▾'}</span>
            </button>
          </div>
          {showExcluded &&
            (data === null ? (
              <LoadState loading emptyText="加载中" />
            ) : data.excluded.length === 0 ? (
              <LoadState loading={false} emptyText="没有剔除项" />
            ) : (
              <div className="max-h-[420px] overflow-auto">
                <table className="grid-table">
                  <thead>
                    <tr>
                      <SortTh sortKey="name" align="left" {...excludedSort}>
                        转债
                      </SortTh>
                      <SortTh sortKey="close" {...excludedSort}>
                        现价
                      </SortTh>
                      <SortTh sortKey="amount" {...excludedSort}>
                        成交额
                      </SortTh>
                      <SortTh sortKey="premium_rt" {...excludedSort}>
                        溢价
                      </SortTh>
                      <SortTh sortKey="reason" align="left" {...excludedSort}>
                        原因
                      </SortTh>
                    </tr>
                  </thead>
                  <tbody>
                    {excludedRows.map((row) => (
                      <tr key={row.code}>
                        <td className="!text-left">
                          <span className="text-fg-muted">{row.name ?? row.code}</span>
                          <span className="num ml-2 text-[12px] text-fg-dim">{row.code}</span>
                        </td>
                        <td className="num text-fg-muted">{fmtNum(row.close, 2)}</td>
                        <td className="num text-fg-muted">{fmtAmount(row.amount)}</td>
                        <td className="num text-fg-muted">{fmtPct(row.premium_rt, 1)}</td>
                        <td className="!text-left text-[12px] text-fg-dim">{row.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
        </Panel>
      </div>
    </Layout>
  )
}
