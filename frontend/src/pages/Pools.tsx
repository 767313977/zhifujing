import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { FirstBoardBoard, FirstBoardTrack, RpsBoard, RpsTrack } from '../api/types'
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
  { key: '113000', label: '11:30 前' },
  { key: '140000', label: '14:00 前' },
]

const CAP_ITEMS = [
  { key: '20-300', label: '20~300 亿' },
  { key: '30-150', label: '30~150 亿' },
  { key: '50-500', label: '50~500 亿' },
]

const THEME_ITEMS = [
  { key: '2', label: '题材 ≥2 家' },
  { key: '3', label: '≥3 家' },
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
  const [before, setBefore] = useState('140000')
  const [cap, setCap] = useState('20-300')
  const [themes, setThemes] = useState('3')
  const [minRps, setMinRps] = useState('0')

  const [firstBoard, setFirstBoard] = useState<FirstBoardBoard | null>(null)
  const [firstBoardError, setFirstBoardError] = useState<string | null>(null)
  const [rps, setRps] = useState<RpsBoard | null>(null)
  const [rpsError, setRpsError] = useState<string | null>(null)
  // 次日验证：参数与上面的候选完全一致，所以「改条件 → 汇总立刻变」
  const [fbTrack, setFbTrack] = useState<FirstBoardTrack | null>(null)
  const [fbTrackOpen, setFbTrackOpen] = useState(false)
  const [rpsTrack, setRpsTrack] = useState<RpsTrack | null>(null)
  const [rpsTrackOpen, setRpsTrackOpen] = useState(false)

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

  useEffect(() => {
    let stale = false
    setFbTrack(null)
    const [minFloatMv, maxFloatMv] = cap.split('-').map(Number)
    api
      .firstBoardTrack({
        before,
        minFloatMv,
        maxFloatMv,
        minThemes: Number(themes),
        days: 20,
      })
      .then((value) => {
        if (!stale) setFbTrack(value)
      })
      .catch(() => {
        // 验证行只是辅助信息，取不到就整行不显示，不弹错
        if (!stale) setFbTrack(null)
      })
    return () => {
      stale = true
    }
  }, [before, cap, themes])

  useEffect(() => {
    let stale = false
    setRpsTrack(null)
    api
      .rpsTrack(Number(minRps), 50, 20)
      .then((value) => {
        if (!stale) setRpsTrack(value)
      })
      .catch(() => {
        if (!stale) setRpsTrack(null)
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
              首板 · 未炸板 ·{' '}
              {firstBoard?.conditions.before
                ? fmtSealTime(String(firstBoard.conditions.before))
                : '14:00:00'}{' '}
              前封板
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
            <span>成交额 ≥3 亿 · 已剔除 ST 与北交所</span>
          </div>

          {fbTrack && (
            <div className="border-b border-line-soft px-4 py-2 text-[12px] text-fg-dim">
              <button
                type="button"
                onClick={() => setFbTrackOpen((open) => !open)}
                className="w-full text-left leading-relaxed hover:text-fg"
              >
                <span className="num">
                  近 {fbTrack.verified_days} 日按当前条件：样本 {fbTrack.samples}
                  {' · '}晋级 {fmtNum(fbTrack.promoted_rate, 1, '%')}
                  {' · '}开盘溢价中位 {fmtPct(fbTrack.open_premium_median)}
                  {' · '}收盘中位 {fmtPct(fbTrack.close_ret_median)}
                  {' · '}大面 {fmtNum(fbTrack.big_loss_rate, 1, '%')}
                  {' · '}一字买不进 {fmtNum(fbTrack.unbuyable_rate, 1, '%')}
                </span>
                {fbTrack.pending_days > 0 && (
                  <span className="ml-1">（{fbTrack.pending_days} 天待验证）</span>
                )}
                <span className="ml-2 text-fg-muted">
                  {fbTrackOpen ? '收起 ▴' : '逐日明细 ▾'}
                </span>
              </button>
              {fbTrackOpen && (
                <div className="mt-2 overflow-x-auto">
                  <table className="grid-table">
                    <thead>
                      <tr>
                        <th className="!text-left">信号日</th>
                        <th className="!text-left">次日</th>
                        <th>候选</th>
                        <th>晋级</th>
                        <th>开盘溢价</th>
                        <th>收盘</th>
                        <th>一字</th>
                        <th>缺日线</th>
                      </tr>
                    </thead>
                    <tbody>
                      {fbTrack.days.map((row) => (
                        <tr key={row.trade_date}>
                          <td className="num !text-left">{row.trade_date}</td>
                          <td className="num !text-left text-fg-muted">
                            {row.next_date ?? '—'}
                          </td>
                          <td className="num">{row.candidates}</td>
                          <td className="num">
                            {row.status === 'pending' ? '待验证' : row.promoted}
                          </td>
                          <td className={`num ${toneOf(row.open_premium_mean)}`}>
                            {fmtPct(row.open_premium_mean)}
                          </td>
                          <td className={`num ${toneOf(row.close_ret_mean)}`}>
                            {fmtPct(row.close_ret_mean)}
                          </td>
                          <td className="num">{row.unbuyable}</td>
                          <td className="num">{row.no_quote}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

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
          {rpsTrack && (
            <div className="border-b border-line-soft px-4 py-2 text-[12px] text-fg-dim">
              <button
                type="button"
                onClick={() => setRpsTrackOpen((open) => !open)}
                className="w-full text-left leading-relaxed hover:text-fg"
              >
                <span className="num">
                  近 {rpsTrack.days} 日榜前 50：1 日 {fmtPct(rpsTrack.ret1.mean)}（超额{' '}
                  {fmtPct(rpsTrack.ret1.excess)}）{' · '}5 日 {fmtPct(rpsTrack.ret5.mean)}
                  （超额 {fmtPct(rpsTrack.ret5.excess)}）{' · '}样本 {rpsTrack.samples}
                </span>
                {rpsTrack.ret1.days < 5 && <span className="ml-2">样本不足 5 天</span>}
                <span className="ml-2 text-fg-muted">
                  {rpsTrackOpen ? '收起 ▴' : '逐日明细 ▾'}
                </span>
              </button>
              {rpsTrackOpen && (
                <div className="mt-2 overflow-x-auto">
                  <table className="grid-table">
                    <thead>
                      <tr>
                        <th className="!text-left">日期</th>
                        <th>只数</th>
                        <th>1 日</th>
                        <th>超额</th>
                        <th>5 日</th>
                        <th>超额</th>
                        <th className="!text-left">状态</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rpsTrack.rows.map((row) => (
                        <tr key={row.trade_date}>
                          <td className="num !text-left">{row.trade_date}</td>
                          <td className="num">{row.picks}</td>
                          <td className={`num ${toneOf(row.ret1)}`}>{fmtPct(row.ret1)}</td>
                          <td className={`num ${toneOf(row.excess1)}`}>{fmtPct(row.excess1)}</td>
                          <td className={`num ${toneOf(row.ret5)}`}>{fmtPct(row.ret5)}</td>
                          <td className={`num ${toneOf(row.excess5)}`}>{fmtPct(row.excess5)}</td>
                          <td className="!text-left text-fg-muted">
                            {row.status === 'pending' ? '待验证' : '已验证'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
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
          阈值（首封时间、市值、题材家数）只是起手参数 —— 改参数看上面那行「近 20 日」
          的样本 / 晋级率 / 开盘溢价怎么变，比凭感觉定阈值靠谱。
        </p>
      </div>
    </Layout>
  )
}
