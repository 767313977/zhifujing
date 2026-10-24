import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { FirstBoardBoard, RpsBoard } from '../api/types'
import Layout from '../components/Layout'
import LoadState from '../components/LoadState'
import Panel from '../components/Panel'
import Segmented from '../components/Segmented'
import StockLink from '../components/StockLink'
import { fmtAmount, fmtNum, fmtPct, fmtSealTime, toneOf } from '../lib/format'

/**
 * 选股池：首板一进二候选 + RPS 相对强度榜。
 *
 * 两块都是**盘后候选池**，不是买卖指令 —— 页面底部与每块表头都写明这一点。
 * 参数放在页面上而不是写死：这些阈值（首封时间、市值、题材家数）没有「标准答案」，
 * 必须拿自己的库回测过再定；界面只负责让它们可调、可复现。
 */

const BEFORE_ITEMS = [
  { key: '100000', label: '10:00 前' },
  { key: '103000', label: '10:30 前' },
  { key: '113000', label: '11:30 前' },
]

const CAP_ITEMS = [
  { key: '30-150', label: '30~150 亿' },
  { key: '50-300', label: '50~300 亿' },
]

const THEME_ITEMS = [
  { key: '3', label: '题材 ≥3 家' },
  { key: '5', label: '≥5 家' },
]

const RPS_ITEMS = [
  { key: '0', label: '全部' },
  { key: '80', label: 'RPS ≥80' },
  { key: '90', label: 'RPS ≥90' },
]

/** RPS 分位的颜色：只有两档极端才染色，中间一律中性（避免满屏红绿）。 */
function rpsTone(value: number | null): string {
  if (value == null) return 'text-fg-muted'
  if (value >= 90) return 'text-up'
  if (value <= 10) return 'text-down'
  return 'text-fg'
}

export default function Pools() {
  const [before, setBefore] = useState('103000')
  const [cap, setCap] = useState('30-150')
  const [themes, setThemes] = useState('3')
  const [minRps, setMinRps] = useState('0')

  const [firstBoard, setFirstBoard] = useState<FirstBoardBoard | null>(null)
  const [firstBoardError, setFirstBoardError] = useState<string | null>(null)
  const [rps, setRps] = useState<RpsBoard | null>(null)
  const [rpsError, setRpsError] = useState<string | null>(null)

  useEffect(() => {
    let stale = false
    setFirstBoard(null)
    setFirstBoardError(null)
    const [minFloatMv, maxFloatMv] = cap.split('-').map(Number)
    api
      .firstBoard({ before, minFloatMv, maxFloatMv, minThemes: Number(themes) })
      .then((value) => {
        if (!stale) setFirstBoard(value)
      })
      .catch((err: Error) => {
        if (!stale) setFirstBoardError(err.message)
      })
    return () => {
      stale = true
    }
  }, [before, cap, themes])

  useEffect(() => {
    let stale = false
    setRps(null)
    setRpsError(null)
    api
      .rpsBoard(Number(minRps), 100)
      .then((value) => {
        if (!stale) setRps(value)
      })
      .catch((err: Error) => {
        if (!stale) setRpsError(err.message)
      })
    return () => {
      stale = true
    }
  }, [minRps])

  const tradeDate = firstBoard?.trade_date ?? rps?.trade_date ?? null

  return (
    <Layout
      toolbar={
        <span className="num hidden text-[13px] text-fg-dim lg:inline">
          {tradeDate ? `${tradeDate} · 盘后数据` : '加载中…'}
        </span>
      }
    >
      <div className="space-y-4">
        <Panel
          title="首板一进二候选"
          meta={
            <span className="num">
              首板 · 未炸板 · {firstBoard?.conditions.before ? fmtSealTime(String(firstBoard.conditions.before)) : '10:30'} 前封板
            </span>
          }
          delay={40}
        >
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-line-soft px-4 py-2.5 text-[12px] text-fg-dim">
            <span className="flex items-center gap-2">
              首封
              <Segmented value={before} items={BEFORE_ITEMS} onChange={setBefore} />
            </span>
            <span className="flex items-center gap-2">
              流通市值
              <Segmented value={cap} items={CAP_ITEMS} onChange={setCap} />
            </span>
            <span className="flex items-center gap-2">
              题材
              <Segmented value={themes} items={THEME_ITEMS} onChange={setThemes} />
            </span>
            <span>成交额 ≥5 亿 · 已剔除 ST 与北交所</span>
          </div>

          {firstBoardError ? (
            <LoadState loading={false} error={firstBoardError} />
          ) : firstBoard === null ? (
            <LoadState loading emptyText="加载中" />
          ) : firstBoard.rows.length === 0 ? (
            <LoadState loading={false} emptyText="当前条件下没有候选 —— 可以放宽首封时间或市值区间" />
          ) : (
            <div className="overflow-x-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th className="!text-left">名称</th>
                    <th>首封</th>
                    <th>封单/流通</th>
                    <th>流通市值</th>
                    <th>成交额</th>
                    <th>换手</th>
                    <th className="!text-left">题材（当日涨停家数）</th>
                    <th className="!text-left">行业</th>
                  </tr>
                </thead>
                <tbody>
                  {firstBoard.rows.map((row) => (
                    <tr key={row.code}>
                      <td className="!text-left">
                        <StockLink code={row.code} className="text-fg">
                          {row.name ?? row.code}
                        </StockLink>
                        <span className="num ml-2 text-[12px] text-fg-dim">{row.code}</span>
                      </td>
                      <td className="num">{fmtSealTime(row.first_seal_time)}</td>
                      <td className={`num ${toneOf(row.seal_ratio)}`}>
                        {fmtNum(row.seal_ratio, 2, '%')}
                      </td>
                      <td className="num">{fmtAmount(row.float_mv)}</td>
                      <td className="num">{fmtAmount(row.amount)}</td>
                      <td className="num">{fmtNum(row.turnover, 2, '%')}</td>
                      <td className="!text-left">
                        {row.theme ? (
                          <>
                            <span className="text-fg">{row.theme}</span>
                            <span className="num ml-1 text-fg-muted">{row.theme_count} 家</span>
                          </>
                        ) : (
                          <span className="text-fg-dim">无精选板块</span>
                        )}
                      </td>
                      <td className="!text-left text-fg-muted">{row.industry ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel
          title="RPS 相对强度榜"
          meta={
            <span className="flex items-center gap-3">
              <Segmented value={minRps} items={RPS_ITEMS} onChange={setMinRps} />
              <span className="num">{rps?.trade_date ?? '—'}</span>
            </span>
          }
          delay={60}
        >
          <p className="border-b border-line-soft px-4 py-2 text-[12px] leading-relaxed text-fg-dim">
            RPS = 区间涨幅在全市场的百分位（0~100，中位秩）。涨幅按日涨跌幅复利计算，
            停牌缺天但覆盖不足 90% 的不参与排名。
          </p>
          {rpsError ? (
            <LoadState loading={false} error={rpsError} />
          ) : rps === null ? (
            <LoadState loading emptyText="加载中" />
          ) : rps.rows.length === 0 ? (
            <LoadState loading={false} emptyText="还没有 RPS 数据（等收盘扫描跑一轮）" />
          ) : (
            <div className="overflow-x-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th className="!text-left">名称</th>
                    <th>收盘</th>
                    <th>当日</th>
                    <th>120 日</th>
                    <th>250 日</th>
                    <th>RPS120</th>
                    <th>RPS250</th>
                  </tr>
                </thead>
                <tbody>
                  {rps.rows.map((row, index) => (
                    <tr key={row.code}>
                      <td className="num text-fg-dim">{index + 1}</td>
                      <td className="!text-left">
                        <StockLink code={row.code} className="text-fg">
                          {row.name ?? row.code}
                        </StockLink>
                        <span className="num ml-2 text-[12px] text-fg-dim">{row.code}</span>
                      </td>
                      <td className="num">{fmtNum(row.close, 2)}</td>
                      <td className={`num ${toneOf(row.pct_chg)}`}>{fmtPct(row.pct_chg)}</td>
                      <td className={`num ${toneOf(row.ret120)}`}>{fmtPct(row.ret120)}</td>
                      <td className={`num ${toneOf(row.ret250)}`}>{fmtPct(row.ret250)}</td>
                      <td className={`num ${rpsTone(row.rps120)}`}>{fmtNum(row.rps120, 1)}</td>
                      <td className={`num ${rpsTone(row.rps250)}`}>{fmtNum(row.rps250, 1)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <p className="px-1 text-[12px] leading-relaxed text-fg-dim">
          两块都是盘后候选池，不是买卖建议：本站没有盘中数据，打板要看次日承接。
          阈值（首封时间、市值、题材家数）只是起手参数，用前请先在自己的库上回测。
        </p>
      </div>
    </Layout>
  )
}
