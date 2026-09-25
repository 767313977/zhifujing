import { Link } from 'react-router-dom'
import type { PatternStock, PatternSummary } from '../api/types'
import { fmtAmount, fmtPct, toneOf } from '../lib/format'
import { rememberStockList } from '../lib/stockNav'
import Panel from './Panel'

/** 首页列几只。首页要的是「今天有什么值得看」，不是完整榜单 */
const TOP_SHOWN = 8

interface PatternPanelProps {
  summary: PatternSummary | null
  stocks: PatternStock[]
  /**
   * 取数失败的原因。必须与「今天没有形态」分开 —— 否则面板里那句
   * 「当日没有命中的形态」会把接口报错说成「今天没有信号」。
   */
  error?: string | null
  delay?: number
}

/**
 * 首页「今日形态」。
 *
 * 排在大盘 / 情绪 / 板块 / 涨停 / 龙虎榜之后、自选股之前：前面几块回答
 * 「今天发生了什么」，这一块回答「明天盯什么」，最后才是自己的票。
 *
 * 个股标签只显示形态名、不显示各自的分数 —— 每行已经有最高分了，
 * 逐个标签再带分数会把这一列撑得很宽，而首页这一列的宽度还要让给涨跌幅。
 * 想看每个形态具体几分，点「全部」去形态页。
 */
export default function PatternPanel({
  summary,
  stocks,
  error = null,
  delay = 0,
}: PatternPanelProps) {
  const counts = summary?.by_pattern ?? []

  return (
    <Panel
      title="今日形态"
      meta={
        <span className="num">
          {summary?.trade_date
            ? `${summary.trade_date} · ${summary.total_hits} 条 / ${summary.total_stocks} 只命中`
            : '—'}
          <Link
            to="/patterns"
            className="ml-3 text-fg-dim transition-colors hover:text-accent"
          >
            全部 →
          </Link>
        </span>
      }
      delay={delay}
    >
      {error ? (
        <div className="px-4 py-8 text-center text-[14px] text-danger">
          形态数据取数失败：{error}
        </div>
      ) : stocks.length === 0 ? (
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">
          当日没有命中的形态（形态扫描在收盘采集之后自动跑）
        </div>
      ) : (
        <>
          {/* 形态家数一排：一眼看出「今天什么形态最普遍」，比只看 Top 8 更有信息量 */}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line-soft px-4 py-2">
            {counts.map((item) => (
              <span key={item.pattern} className="num text-[13px] text-fg-dim">
                {item.pattern_name}
                <span className={`ml-1 ${item.stocks > 0 ? 'text-fg-muted' : 'text-fg-dim'}`}>
                  {item.stocks}
                </span>
              </span>
            ))}
          </div>

          {/* 表格必须套一层 overflow-auto：否则窄视口下多列会把**整个页面**撑出
              横向滚动条（实测 550px 宽时溢出 109px），而不是让表格自己滚。
              这是全站表格的惯例，见 LimitTable / WatchlistPanel */}
          <div className="overflow-auto">
            <table className="grid-table">
              <thead>
                <tr>
                  <th>评分</th>
                  <th className="!text-left">代码 / 名称</th>
                  <th className="!text-left">命中形态</th>
                  <th>涨跌幅</th>
                  <th title="近 20 日日均成交额">日均额</th>
                  <th title="总市值：是题材小票还是权重">市值</th>
                </tr>
              </thead>
              <tbody>
                {stocks.slice(0, TOP_SHOWN).map((stock) => (
                  <tr
                    key={stock.code}
                    onClick={() => {
                      // 存的是首页这一小份（不是完整榜单），所以前后翻只在这 8 只里走
                      rememberStockList(stocks.slice(0, TOP_SHOWN).map((item) => item.code))
                    }}
                  >
                    <td>
                      <span
                        className={`num ${stock.score >= 95 ? 'text-accent' : 'text-fg-muted'}`}
                      >
                        {stock.score.toFixed(1)}
                      </span>
                    </td>
                    <td className="!text-left">
                      <Link
                        to={`/stock/${stock.code}`}
                        className="transition-colors hover:text-accent"
                      >
                        <span className="num text-fg-dim">{stock.code}</span>
                        <span className="ml-2 text-fg">{stock.name ?? '—'}</span>
                      </Link>
                    </td>
                    <td className="!text-left">
                      <span className="text-[13px] text-fg-muted">
                        {stock.patterns.map((item) => item.pattern_name).join(' · ')}
                      </span>
                    </td>
                    <td>
                      <span className={`num ${toneOf(stock.pct_chg)}`}>
                        {fmtPct(stock.pct_chg)}
                      </span>
                    </td>
                    <td>
                      <span className="num text-fg-muted">{fmtAmount(stock.avg_amount)}</span>
                    </td>
                    <td>
                      <span className="num text-fg-dim">{fmtAmount(stock.total_mv)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Panel>
  )
}
