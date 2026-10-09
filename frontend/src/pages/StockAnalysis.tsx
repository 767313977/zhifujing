import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { StockAnalysis, StockNews } from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import PhasePanel from '../components/PhasePanel'
import { fmtNum, fmtPct, toneOf } from '../lib/format'

/**
 * 个股分析（移植自 zhaohuibin7/yangban-desk 的「查票分析」）。
 *
 * 一个输入框把「代码 / 名称 / 拼音首字母」解析成结论 —— 手边想到什么就敲什么
 * （`300654` / `世纪天鸿` / `sjth` 都行）。结论本身来自 `services/patterns.classify_phase`，
 * 与「悟道之路」那几张名单**共用同一批判据**，所以两处不会打架。
 *
 * 「技术 / 新闻 / 基本面三面合议」三面都齐了：技术＝结论卡，基本面＝行业 + 复盘关联，
 * 新闻＝下面单独一块（**东财口径**，每次分析现取）。新闻是**慢且可能失败**的那一面，
 * 所以独立请求、独立的加载与失败提示，不拖累结论。
 */
export default function StockAnalysisPage() {
  const [query, setQuery] = useState('')
  const [result, setResult] = useState<StockAnalysis | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [news, setNews] = useState<StockNews | null>(null)
  const [newsLoading, setNewsLoading] = useState(false)

  const run = useCallback(async (raw: string) => {
    const q = raw.trim()
    if (!q) {
      setError('请输入股票代码、名称或拼音首字母')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const found = await api.analysisLookup(q)
      setResult(found)
      // 新闻单独再发一次：它要出外网、慢且可能失败，别让它拖住结论；
      // 失败也不弹错误条 —— 那一块自己显示原因就够了
      setNews(null)
      setNewsLoading(true)
      api
        .stockNews(found.code)
        .then(setNews)
        .catch((err: Error) =>
          setNews({
            code: found.code,
            name: found.name,
            rows: [],
            hidden: 0,
            note: `新闻取不到：${err.message}`,
          }),
        )
        .finally(() => setNewsLoading(false))
    } catch (err) {
      // 解析不出 / 匹配到多只时后端给的就是一句能直接读的话，原样显示；
      // **不清空上一次的结果** —— 敲错一个字不该把刚查到的东西抹掉
      setError((err as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  return (
    <Layout>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}

      <div className="space-y-4">
        <Panel
          title="查票分析"
          meta={<span className="text-[13px] text-fg-dim">代码 / 名称 / 拼音首字母</span>}
          delay={30}
        >
          <div className="space-y-2.5 px-4 py-3.5">
            <form
              className="flex flex-wrap items-center gap-2"
              onSubmit={(event) => {
                event.preventDefault()
                void run(query)
              }}
            >
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="如 300654 / 世纪天鸿 / sjth"
                autoFocus
                className="num w-64 border border-line bg-transparent px-3 py-2 text-[14px] text-fg outline-none placeholder:text-fg-dim focus:border-line-soft"
              />
              <button
                type="submit"
                disabled={loading}
                className="border border-accent/60 bg-accent/10 px-4 py-2 text-[13px] text-accent transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {loading ? '分析中…' : '分析这只'}
              </button>
              {result && (
                <span className="num text-[13px] text-fg-muted">
                  {result.name} · {result.phase?.label ?? '暂无判定'} ·{' '}
                  {result.trade_date ?? '无日线'}
                </span>
              )}
            </form>
            <p className="text-[12px] leading-relaxed text-fg-dim">
              分析里带技术 / 新闻 / 基本面三面合议（旁证）：技术面＝下面的结论卡，
              基本面＝行业与复盘关联，新闻＝东财那条源（每次分析现取；只列标题里出现这只票的，
              名单类稿件按条数标出、不混在里面）。
            </p>
          </div>
        </Panel>

        {result && (
          <>
            <Panel
              title={`${result.name ?? result.code}  ${result.code}`}
              meta={
                <span className="num text-[13px]">
                  {result.trade_date ?? '—'}
                  <span className="mx-2">·</span>
                  <span className={toneOf(result.pct_chg)}>{fmtPct(result.pct_chg)}</span>
                  <Link
                    to={`/stock/${result.code}`}
                    className="ml-3 text-fg-dim transition-colors hover:text-accent"
                  >
                    看完整个股页（K 线 / 资金）→
                  </Link>
                </span>
              }
              delay={45}
            >
              <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 px-4 py-3 text-[13px] md:grid-cols-3">
                <div className="flex gap-2">
                  <span className="text-fg-dim">收盘</span>
                  <span className={`num ${toneOf(result.pct_chg)}`}>
                    {fmtNum(result.close, 2)}
                  </span>
                </div>
                <div className="flex gap-2">
                  <span className="text-fg-dim">所属行业</span>
                  <span className="text-fg-muted">{result.industry ?? '—'}</span>
                </div>
                <div className="flex gap-2">
                  <span className="text-fg-dim">精选板块</span>
                  <span className="text-fg-muted">
                    {result.sectors.length > 0 ? result.sectors.join('、') : '—（没涨停过就没有）'}
                  </span>
                </div>
                <div className="flex gap-2">
                  <span className="text-fg-dim">复盘关联</span>
                  <span className="num text-fg-muted">
                    涨停 {result.limit_up_count} 次 · 龙虎榜 {result.lhb_count} 次
                  </span>
                </div>
              </div>
            </Panel>

            {result.phase ? (
              <PhasePanel phase={result.phase} title="结论" delay={60} />
            ) : (
              <Panel title="结论" delay={60}>
                <div className="px-4 py-8 text-center text-[14px] text-fg-dim">
                  本地日线不足 25 根（次新或还没缓存），没法按这套模板给结论
                </div>
              </Panel>
            )}

            <NewsPanel news={news} loading={newsLoading} />
          </>
        )}
      </div>
    </Layout>
  )
}

/**
 * 新闻那一块（「三面合议」里的新闻面）。
 *
 * 三条状态都要说清楚：加载中 / 有内容 / 空（**且区分「真没搜到」与「源没返回」**）——
 * 把后者显示成「暂无新闻」会让人以为这只票很干净。
 * `hidden` 要标出来：那些是「只在正文表格里提到代码」的名单类稿件，有标题命中的新闻时
 * 后端把它们筛掉了 —— 不写一句，看起来就像源少给了数据。
 * 标题直接链到东财原文，`noopener` 必须带（`noreferrer` 是它的现代写法）。
 */
function NewsPanel({ news, loading }: { news: StockNews | null; loading: boolean }) {
  return (
    <Panel
      title="新闻"
      meta={
        <span className="text-[13px] text-fg-dim">
          东财口径
          {news && news.rows.length > 0 && <span className="num ml-2">{news.rows.length} 条</span>}
          {news && news.hidden > 0 && (
            <span className="num ml-2">另隐去 {news.hidden} 条只在正文提到代码的名单稿</span>
          )}
        </span>
      }
      delay={75}
    >
      {loading ? (
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">加载中…</div>
      ) : !news || news.rows.length === 0 ? (
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">
          {news?.note ?? '暂无新闻'}
        </div>
      ) : (
        <ul className="divide-y divide-line-soft">
          {news.rows.map((row) => (
            <li key={`${row.published_at}-${row.url}`} className="px-4 py-2.5">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <a
                  href={row.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-[13px] text-fg transition-colors hover:text-accent"
                >
                  {row.title}
                </a>
                <span className="num text-[12px] text-fg-dim">
                  {row.published_at}
                  <span className="mx-1.5">·</span>
                  {row.source}
                </span>
              </div>
              {row.summary && (
                <p className="mt-1 text-[12px] leading-relaxed text-fg-dim">{row.summary}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}
