import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { CbRadar, CbRadarRow, CbScreen, CbScreenQuery, CbScreenRow } from '../api/types'
import Layout from '../components/Layout'
import LoadState from '../components/LoadState'
import Panel from '../components/Panel'
import Segmented from '../components/Segmented'
import SortTh from '../components/SortTh'
import StockLink from '../components/StockLink'
import { fmtAmount, fmtNum, fmtPct, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'

/**
 * 可转债：筛选 + 强赎雷达。
 *
 * 口径（页面顶部必须写出来，否则会被当成实时数据拿去下单）：
 *
 * - **盘后数据**：每天 17:30 那趟采一次快照（新浪行情 + 东财条款 + 集思录强赎），
 *   三个来源都只有实时行情、**不能回补历史**，所以 `as_of` 只会是最近一个采集日；
 * - **规模**：优先用剩余规模；集思录没这条记录的券按「未知」处理，不拿发行规模顶替；
 * - **YTM**：用票息原文 + 到期赎回价算的**复利 IRR**，未计税；票息没补齐时显示「—」；
 * - **强赎 / 下修计数**：近 30 个交易日的转股价值越过条款阈值的天数。历史不够 30 天时
 *   显示「≥n」，不假装精确；
 * - 风险标签只提示，**不默认剔除**（只有「双低」预设默认剔 ST）。
 */

const PRESET_ITEMS = [
  { key: 't0', label: '日内 T+0' },
  { key: 'double_low', label: '双低' },
  { key: 'low_premium', label: '低溢价' },
  { key: 'bond_floor', label: '债底' },
  { key: 'revise', label: '博下修' },
]

const RATING_ITEMS = [
  { key: '', label: '不限' },
  { key: 'AA', label: '≥AA' },
  { key: 'AA-', label: '≥AA-' },
  { key: 'A+', label: '≥A+' },
]

/** 一个筛选输入框（数字）。空串 = 不设条件。 */
function NumberField({
  label,
  value,
  onChange,
  placeholder,
  suffix,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  placeholder: string
  suffix?: string
}) {
  return (
    <label className="flex items-center gap-1.5">
      <span>{label}</span>
      <input
        type="number"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="num w-[72px] border border-line bg-ink-850 px-1.5 py-[3px] text-[12px] text-fg outline-none focus:border-fg-dim"
      />
      {suffix && <span className="text-fg-dim">{suffix}</span>}
    </label>
  )
}

function Toggle({
  label,
  value,
  onChange,
}: {
  label: string
  value: boolean
  onChange: (value: boolean) => void
}) {
  return (
    <label className="flex cursor-pointer items-center gap-1.5">
      <input
        type="checkbox"
        checked={value}
        onChange={(event) => onChange(event.target.checked)}
        className="accent-current"
      />
      {label}
    </label>
  )
}

/** 筛选表的排序口径（后端已按预设排好，前端只是把能力补齐）。 */
const SCREEN_SORTS: SortSpecs<CbScreenRow> = {
  score: { value: (row) => row.score },
  bond_name: { value: (row) => row.bond_name ?? row.bond_code, first: 'asc' },
  close: { value: (row) => row.close },
  pct_chg: { value: (row) => row.pct_chg },
  premium_pct: { value: (row) => row.premium_pct, first: 'asc' },
  double_low: { value: (row) => row.double_low, first: 'asc' },
  conv_value: { value: (row) => row.conv_value },
  ytm_pct: { value: (row) => row.ytm_pct },
  remain_size: { value: (row) => row.remain_size },
  years_left: { value: (row) => row.years_left },
  rating: { value: (row) => row.rating, first: 'asc' },
  amount: { value: (row) => row.amount },
  stock_pct_chg: { value: (row) => row.stock_pct_chg },
  redeem_count: { value: (row) => row.redeem_count },
  revise_count: { value: (row) => row.revise_count },
}

/** 雷达表的排序口径。 */
const RADAR_SORTS: SortSpecs<CbRadarRow> = {
  bond_name: { value: (row) => row.bond_name ?? row.bond_code, first: 'asc' },
  close: { value: (row) => row.close },
  premium_pct: { value: (row) => row.premium_pct, first: 'asc' },
  redeem_status: { value: (row) => row.redeem_status, first: 'asc' },
  last_trade_date: { value: (row) => row.last_trade_date, first: 'asc' },
  days_left: { value: (row) => row.days_left, first: 'asc' },
  redeem_count: { value: (row) => row.redeem_count },
}

const NO_ROWS: CbScreenRow[] = []
const NO_RADAR: CbRadarRow[] = []

/** 计数：窗口不完整时写「≥n」，别假装精确。 */
function countText(value: number | null, complete: boolean): string {
  if (value == null) return '—'
  return complete ? String(value) : `≥${value}`
}

export default function Convertible() {
  const [tab, setTab] = useState<'screen' | 'radar'>('screen')
  const [preset, setPreset] = useState('t0')

  // 覆盖条件：空串 = 不设条件（交给后端预设默认值）
  const [priceMax, setPriceMax] = useState('')
  const [premiumMax, setPremiumMax] = useState('')
  const [sizeMin, setSizeMin] = useState('')
  const [amountMin, setAmountMin] = useState('')
  const [ratingMin, setRatingMin] = useState('')
  const [excludeRedeem, setExcludeRedeem] = useState(true)
  const [excludeSt, setExcludeSt] = useState(true)

  const [data, setData] = useState<CbScreen | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [radar, setRadar] = useState<CbRadar | null>(null)
  const [radarError, setRadarError] = useState<string | null>(null)

  const params = useMemo<CbScreenQuery>(() => {
    const query: CbScreenQuery = { preset, exclude_redeem: excludeRedeem, exclude_st: excludeSt }
    const numbers: [keyof CbScreenQuery, string][] = [
      ['price_max', priceMax],
      ['premium_max', premiumMax],
      ['size_min', sizeMin],
      ['amount_min', amountMin],
    ]
    for (const [key, text] of numbers) {
      const value = Number(text)
      if (text !== '' && Number.isFinite(value)) {
        // 成交额输入按「万」填，接口按元收
        ;(query as Record<string, unknown>)[key] =
          key === 'amount_min' ? value * 1e4 : value
      }
    }
    if (ratingMin) query.rating_min = ratingMin
    return query
  }, [preset, priceMax, premiumMax, sizeMin, amountMin, ratingMin, excludeRedeem, excludeSt])

  useEffect(() => {
    if (tab !== 'screen') return
    let stale = false
    setError(null)
    api
      .cbScreen(params)
      .then((value) => {
        if (!stale) setData(value)
      })
      .catch((err: Error) => {
        if (!stale) setError(err.message)
      })
    return () => {
      stale = true
    }
  }, [tab, params])

  useEffect(() => {
    if (tab !== 'radar' || radar !== null) return
    let stale = false
    setRadarError(null)
    api
      .cbRadar()
      .then((value) => {
        if (!stale) setRadar(value)
      })
      .catch((err: Error) => {
        if (!stale) setRadarError(err.message)
      })
    return () => {
      stale = true
    }
  }, [tab, radar])

  const [sort, rows] = useSort(data?.items ?? NO_ROWS, SCREEN_SORTS, { key: null })
  const [radarSort, radarRows] = useSort(radar?.items ?? NO_RADAR, RADAR_SORTS, { key: null })

  const asOf = data?.as_of ?? radar?.as_of ?? null

  return (
    <Layout
      toolbar={
        <span className="num hidden text-[13px] text-fg-dim lg:inline">
          {asOf ? `${asOf} · 盘后快照` : '加载中…'}
        </span>
      }
    >
      <div className="space-y-4">
        <Panel
          title="可转债"
          meta={
            <span className="flex items-center gap-3">
              <Segmented
                value={tab}
                items={[
                  { key: 'screen', label: '筛选' },
                  { key: 'radar', label: '强赎雷达' },
                ]}
                onChange={(value) => setTab(value as 'screen' | 'radar')}
              />
              <span className="num text-fg-dim">
                {tab === 'screen'
                  ? data
                    ? `命中 ${data.total} 只`
                    : '—'
                  : radar
                    ? `${radar.items.length} 只`
                    : '—'}
              </span>
            </span>
          }
          delay={40}
        >
          <p className="border-b border-line-soft px-4 py-2 text-[12px] leading-relaxed text-fg-dim">
            盘后数据，截至 {asOf ?? '—'}；规模优先用
            <span className="text-fg-muted"> 剩余规模 </span>
            （缺了就是「—」，不拿发行规模顶替）；YTM 是票息 + 到期赎回价算的
            <span className="text-fg-muted"> 复利 IRR </span>
            ，未计税。强赎 / 下修计数看近 30 个交易日的转股价值，历史不足 30 天时显示「≥n」。
          </p>

          {tab === 'screen' ? (
            <>
              <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-line-soft px-4 py-2 text-[12px] text-fg-dim">
                <Segmented value={preset} items={PRESET_ITEMS} onChange={setPreset} />
                <NumberField label="价格≤" value={priceMax} onChange={setPriceMax} placeholder="不限" />
                <NumberField
                  label="溢价≤"
                  value={premiumMax}
                  onChange={setPremiumMax}
                  placeholder="不限"
                  suffix="%"
                />
                <NumberField label="剩余≥" value={sizeMin} onChange={setSizeMin} placeholder="不限" suffix="亿" />
                <NumberField
                  label="成交额≥"
                  value={amountMin}
                  onChange={setAmountMin}
                  placeholder="不限"
                  suffix="万"
                />
                <span className="flex items-center gap-2">
                  评级
                  <Segmented value={ratingMin} items={RATING_ITEMS} onChange={setRatingMin} />
                </span>
                <Toggle label="剔除强赎" value={excludeRedeem} onChange={setExcludeRedeem} />
                <Toggle label="剔除 ST" value={excludeSt} onChange={setExcludeSt} />
              </div>

              {error ? (
                <LoadState loading={false} error={error} />
              ) : data === null ? (
                <LoadState loading emptyText="加载中" />
              ) : data.items.length === 0 ? (
                <LoadState
                  loading={false}
                  emptyText={
                    data.as_of === null
                      ? '还没有可转债数据 —— 收盘后 17:30 那趟才会采'
                      : '当前条件下没有可转债 —— 放宽价格 / 溢价 / 规模再看'
                  }
                />
              ) : (
                <div className="overflow-x-auto">
                  <table className="grid-table">
                    <thead>
                      <tr>
                        {preset === 't0' && (
                          <SortTh sortKey="score" title="正股涨幅 30% + 换手 25% + 低溢价 20% + 振幅 15% + 小盘 10%" {...sort}>
                            评分
                          </SortTh>
                        )}
                        <SortTh sortKey="bond_name" align="left" {...sort}>
                          转债
                        </SortTh>
                        <SortTh sortKey="stock_pct_chg" align="left" {...sort}>
                          正股
                        </SortTh>
                        <SortTh sortKey="close" {...sort}>
                          价格
                        </SortTh>
                        <SortTh sortKey="pct_chg" {...sort}>
                          涨跌
                        </SortTh>
                        <SortTh sortKey="premium_pct" title="转股溢价率，越低跟涨越紧" {...sort}>
                          溢价
                        </SortTh>
                        <SortTh sortKey="double_low" title="双低 = 价格 + 转股溢价率" {...sort}>
                          双低
                        </SortTh>
                        <SortTh sortKey="conv_value" title="100 ÷ 转股价 × 正股价" {...sort}>
                          转股价值
                        </SortTh>
                        <SortTh sortKey="ytm_pct" title="到期 YTM（复利 IRR，未计税）" {...sort}>
                          YTM
                        </SortTh>
                        <SortTh sortKey="remain_size" title="剩余规模（亿）；集思录没记录时不显示" {...sort}>
                          剩余
                        </SortTh>
                        <SortTh sortKey="years_left" {...sort}>
                          年限
                        </SortTh>
                        <SortTh sortKey="rating" {...sort}>
                          评级
                        </SortTh>
                        <SortTh sortKey="amount" {...sort}>
                          成交额
                        </SortTh>
                        <SortTh sortKey="redeem_count" title="近 30 日转股价值 ≥ 130% 的天数" {...sort}>
                          强赎
                        </SortTh>
                        <SortTh sortKey="revise_count" title="近 30 日转股价值 ≤ 85% 的天数" {...sort}>
                          下修
                        </SortTh>
                        <th className="!text-left">标签</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row) => (
                        <tr key={row.bond_code}>
                          {preset === 't0' && (
                            <td className="num">{row.score == null ? '—' : fmtNum(row.score, 1)}</td>
                          )}
                          <td className="!text-left">
                            <span className="text-fg">{row.bond_name ?? row.bond_code}</span>
                            <span className="num ml-2 text-[11px] text-fg-dim">{row.bond_code}</span>
                          </td>
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
                          <td className="num">{fmtNum(row.close, 2)}</td>
                          <td className={`num ${toneOf(row.pct_chg)}`}>{fmtPct(row.pct_chg)}</td>
                          <td className="num">{fmtPct(row.premium_pct, 1)}</td>
                          <td className="num text-fg-muted">{fmtNum(row.double_low, 1)}</td>
                          <td className="num text-fg-muted">{fmtNum(row.conv_value, 1)}</td>
                          <td className={`num ${toneOf(row.ytm_pct)}`}>{fmtPct(row.ytm_pct, 1)}</td>
                          <td className="num">
                            {row.remain_size == null ? '—' : `${fmtNum(row.remain_size, 1)} 亿`}
                          </td>
                          <td className="num text-fg-muted">{fmtNum(row.years_left, 1)}</td>
                          <td className="num text-fg-muted">{row.rating ?? '—'}</td>
                          <td className="num text-fg-muted">{fmtAmount(row.amount)}</td>
                          <td className="num text-fg-muted">
                            {countText(row.redeem_count, row.count_complete)}
                          </td>
                          <td className="num text-fg-muted">
                            {countText(row.revise_count, row.count_complete)}
                          </td>
                          <td className="!text-left">
                            {row.labels.map((label) => (
                              <span
                                key={label}
                                className={`mr-1 whitespace-nowrap rounded border border-line-soft px-1 text-[11px] ${
                                  label === '已公告强赎' ? 'text-danger' : 'text-warn'
                                }`}
                              >
                                {label}
                              </span>
                            ))}
                            {row.risks.map((risk) => (
                              <span
                                key={risk}
                                className="mr-1 whitespace-nowrap rounded border border-line-soft px-1 text-[11px] text-fg-dim"
                                title="风险提示，不默认剔除"
                              >
                                {risk}
                              </span>
                            ))}
                            {row.labels.length === 0 && row.risks.length === 0 && (
                              <span className="text-fg-dim">—</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <p className="px-4 py-3 text-[12px] leading-relaxed text-fg-dim">
                预设口径：<span className="text-fg-muted">日内 T+0</span> 先过五道硬门槛
                （强赎 / 价格 100~150 / 溢价 ≤30% / 成交额 ≥5000 万 / 剩余 ≥3 亿、剩余 &gt;1 年、
                评级 ≥AA-）再按五因子打分；<span className="text-fg-muted">双低</span> 剔强赎与 ST、
                按「价格 + 溢价」升序；<span className="text-fg-muted">低溢价</span> 价格 ≤130、按溢价升序；
                <span className="text-fg-muted">债底</span> 只要 YTM &gt; 0，按 YTM 降序；
                <span className="text-fg-muted">博下修</span> 下修计数 ≥10 且剩余 ≤3 年。
                盘后候选，不是买卖建议。
              </p>
            </>
          ) : radarError ? (
            <LoadState loading={false} error={radarError} />
          ) : radar === null ? (
            <LoadState loading emptyText="加载中" />
          ) : radar.items.length === 0 ? (
            <LoadState loading={false} emptyText="今天没有强赎相关的转债" />
          ) : (
            <>
              <p className="border-b border-line-soft px-4 py-2 text-[12px] leading-relaxed text-fg-dim">
                已公告强赎的按<span className="text-fg-muted">最后交易日升序</span>排在前面 ——
                公告之后按约 100 元赎回，没转股的会在几天里亏掉 20%~30%。「强赎临近」是近 30 日
                转股价值站上 130% 的天数（≥10 天开始预警，条款阈值是 15/30）。
              </p>
              <div className="overflow-x-auto">
                <table className="grid-table">
                  <thead>
                    <tr>
                      <SortTh sortKey="bond_name" align="left" {...radarSort}>
                        转债
                      </SortTh>
                      <SortTh sortKey="close" {...radarSort}>
                        价格
                      </SortTh>
                      <SortTh sortKey="premium_pct" {...radarSort}>
                        溢价
                      </SortTh>
                      <SortTh sortKey="redeem_status" align="left" {...radarSort}>
                        状态
                      </SortTh>
                      <SortTh sortKey="last_trade_date" title="强赎完成日 / 最后交易日" {...radarSort}>
                        最后交易日
                      </SortTh>
                      <SortTh sortKey="days_left" {...radarSort}>
                        剩余天数
                      </SortTh>
                      <SortTh sortKey="redeem_count" title="近 30 日转股价值 ≥ 130% 的天数" {...radarSort}>
                        强赎计数
                      </SortTh>
                    </tr>
                  </thead>
                  <tbody>
                    {radarRows.map((row) => (
                      <tr key={row.bond_code}>
                        <td className="!text-left">
                          <span className="text-fg">{row.bond_name ?? row.bond_code}</span>
                          <span className="num ml-2 text-[11px] text-fg-dim">{row.bond_code}</span>
                          {row.stock_name && (
                            <span className="ml-2 text-[11px] text-fg-dim">/{row.stock_name}</span>
                          )}
                        </td>
                        <td className="num">{fmtNum(row.close, 2)}</td>
                        <td className="num">{fmtPct(row.premium_pct, 1)}</td>
                        <td className="!text-left text-[12px]">
                          <span
                            className={
                              row.redeem_status?.includes('已公告') ? 'text-danger' : 'text-fg-muted'
                            }
                          >
                            {row.redeem_status ?? '—'}
                          </span>
                        </td>
                        <td className="num text-fg-muted">{row.last_trade_date ?? '—'}</td>
                        <td
                          className={`num ${
                            row.days_left != null && row.days_left <= 10 ? 'text-danger' : 'text-fg-muted'
                          }`}
                        >
                          {row.days_left == null ? '—' : `${row.days_left} 天`}
                        </td>
                        <td className="num text-fg-muted">
                          {countText(row.redeem_count, row.count_complete)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </Panel>
      </div>
    </Layout>
  )
}
