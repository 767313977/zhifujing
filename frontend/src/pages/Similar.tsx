import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { SimilarDays } from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import { fmtNum, fmtPct, toneOf } from '../lib/format'

/**
 * 历史相似日：用当天的情绪向量（涨停数 / 炸板率 / 最高板 / 站上 MA20 / 恐贪）
 * 找历史上最像的几天，看它们之后 1/3/5 个交易日指数与情绪怎么走。
 *
 * **只是参照，不是预测**：页面上的每一列都是已经发生过的历史。
 */
export default function SimilarPage() {
  const [date, setDate] = useState('')
  const [data, setData] = useState<SimilarDays | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // 连点「查询」时只认最后一次的结果，避免慢请求后到覆盖新结果
  const seqRef = useRef(0)

  const load = useCallback(async (target?: string) => {
    const seq = ++seqRef.current
    setLoading(true)
    setError(null)
    try {
      const result = await api.similarDays(target || undefined)
      if (seq !== seqRef.current) return
      setData(result)
    } catch (err) {
      if (seq !== seqRef.current) return
      setError((err as Error).message)
      setData(null)
    } finally {
      if (seq === seqRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const toolbar = (
    <div className="flex items-center gap-2">
      <input
        type="date"
        value={date}
        onChange={(event) => setDate(event.target.value)}
        className="num border border-line bg-ink-900 px-2 py-[3px] text-[13px] text-fg outline-none focus:border-fg-dim"
      />
      <button
        type="button"
        onClick={() => void load(date)}
        className="border border-line px-2.5 py-[3px] text-[13px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg"
      >
        查询
      </button>
      <button
        type="button"
        onClick={() => {
          setDate('')
          void load()
        }}
        className="text-[13px] text-accent hover:underline"
      >
        最新
      </button>
    </div>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}

      <div className="space-y-4">
        <Panel
          title="当天的情绪向量"
          meta={
            <span className="num">
              {loading
                ? '加载中…'
                : data?.trade_date
                  ? `${data.trade_date} · 回看 ${data.lookback_days} 个交易日`
                  : '—'}
            </span>
          }
          delay={40}
        >
          {!data || data.vector.length === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {loading ? '加载中…' : '库里还没有情绪数据'}
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-2 px-4 py-3 sm:grid-cols-5">
              {data.vector.map((item) => (
                <div key={item.key} className="border border-line-soft px-3 py-2">
                  <div className="text-[11px] text-fg-dim">{item.label}</div>
                  <div className="num text-[17px] text-fg">
                    {item.value == null ? '—' : fmtNum(item.value, 2)}
                  </div>
                  <div className="num text-[11px] text-fg-dim">
                    {item.rank == null ? '无分位' : `历史分位 ${(item.rank * 100).toFixed(0)}%`}
                  </div>
                </div>
              ))}
            </div>
          )}
          <p className="px-4 pb-3 text-[12px] leading-relaxed text-fg-dim">
            五维先各自转成「在过去一年多的历史里排多高」（0~100%），再算欧氏距离 ——
            量纲不同的原始值直接比会被数值大的维度带偏。缺数据的维度不参与，至少三维相同才算可比。
          </p>
        </Panel>

        <Panel
          title="历史上最像的几天"
          meta={<span className="num">相似度 = (1 − 距离) × 100 · 只作参照，不是预测</span>}
          delay={70}
        >
          {!data || data.rows.length === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
              {loading ? '加载中…' : '没有可比的历史日子（数据还不够）'}
            </div>
          ) : (
            <div className="overflow-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th className="!text-left">日期</th>
                    <th>相似度</th>
                    <th>涨停</th>
                    <th>炸板率</th>
                    <th>最高板</th>
                    <th>站上 MA20</th>
                    <th>恐贪</th>
                    <th>指数 1 日</th>
                    <th>指数 3 日</th>
                    <th>指数 5 日</th>
                    <th>情绪 1 日</th>
                    <th>情绪 3 日</th>
                    <th>情绪 5 日</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((row) => (
                    <tr key={row.trade_date}>
                      <td className="!text-left">
                        <span className="num text-accent">{row.trade_date}</span>
                      </td>
                      <td>
                        <span className="num text-fg">{row.similarity.toFixed(1)}%</span>
                      </td>
                      <td>
                        <span className="num">{row.limit_up ?? '—'}</span>
                      </td>
                      <td>
                        <span className="num">
                          {row.broken_rate == null ? '—' : `${row.broken_rate.toFixed(1)}%`}
                        </span>
                      </td>
                      <td>
                        <span className="num">{row.max_consecutive ?? '—'}</span>
                      </td>
                      <td>
                        <span className="num">
                          {row.above_ma20 == null ? '—' : `${row.above_ma20.toFixed(1)}%`}
                        </span>
                      </td>
                      <td>
                        <span className="num">
                          {row.fear_greed == null ? '—' : row.fear_greed.toFixed(1)}
                        </span>
                      </td>
                      {[row.index_1d, row.index_3d, row.index_5d].map((value, index) => (
                        <td key={`i${index}`}>
                          <span className={`num ${toneOf(value)}`}>{fmtPct(value)}</span>
                        </td>
                      ))}
                      {[row.sentiment_1d, row.sentiment_3d, row.sentiment_5d].map(
                        (value, index) => (
                          <td key={`s${index}`}>
                            <span className="num text-fg-muted">
                              {value == null ? '—' : value.toFixed(1)}
                            </span>
                          </td>
                        ),
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="px-4 py-3 text-[12px] leading-relaxed text-fg-dim">
            指数收益是上证指数在相似日之后第 1/3/5 个交易日的涨跌幅；情绪是接力温度分数
            （0~100，越高越热）。还没走到对应交易日的格子显示「—」，不按 0 编造。
            相似日只回答「历史上类似的日子后来怎么了」，
            <span className="text-fg-muted">不构成对明天的预测</span>。
          </p>
        </Panel>
      </div>
    </Layout>
  )
}
