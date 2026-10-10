import { useCallback, useRef, useState } from 'react'
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
 * 与「悟道之路」那几张名单**共用同一批判据**。
 *
 * ⚠️ 但**不能因此说「两处不会打架」**（2026-10-09 起）：名单只收创业板 + 科创板、
 * 且出口多一道 `_wudao_leave_ok`（收盘离开最高 ≤ 2.9%），两处都**故意**没进这里 ——
 * 所以这一页说「明天盯 / 明天预案」时，名单里**可能没有它**（实测 09-28：31 只里只有 6 只在名单）。
 * 详见 `api/analysis.py` 的模块说明。
 *
 * 「技术 / 新闻 / 基本面」三面都摆出来了：技术＝结论卡，基本面＝行业 + 复盘关联，
 * 新闻＝下面单独一块（**东财口径**，每次分析现取）。⚠️ 三面是**并列展示、不是真的合议**
 * （没有任何一步把三者综合成一个判断）；而「基本面」这一面其实只有**行业名 + 两个计数**，
 * 没有盈利/估值/现金流 —— 页面文案别把它说成做过基本面分析。
 * 新闻是**慢且可能失败**的那一面，所以独立请求、独立的加载与失败提示，不拖累结论。
 */
export default function StockAnalysisPage() {
  const [query, setQuery] = useState('')
  const [result, setResult] = useState<StockAnalysis | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [news, setNews] = useState<StockNews | null>(null)
  const [newsLoading, setNewsLoading] = useState(false)
  // 最近一次请求的票。新闻请求是**单独发、可能慢**的：连查两只票时，先那只的响应
  // 可能后到，会把后那只的新闻**覆盖**掉，`finally` 也会提前把加载态关掉（显示成
  // 「暂无新闻」）。所有回调都先比对这个值，只认当前这只票的结果。
  const newsCodeRef = useRef<string | null>(null)
  // 主查询的序号守卫。newsCodeRef 只护得住新闻那一块，护不住 `result`（结论卡）——
  // 连查两只票时先发的若后返回，会把页面上的结论覆盖成**另一只票**。每次 run 自增一次，
  // 回调先比对序号，只认最新那次请求（新闻那三条状态也一起受它保护）。
  const runSeqRef = useRef(0)

  const run = useCallback(async (raw: string) => {
    const q = raw.trim()
    if (!q) {
      setError('请输入股票代码、名称或拼音首字母')
      return
    }
    const seq = ++runSeqRef.current
    setLoading(true)
    setError(null)
    try {
      const found = await api.analysisLookup(q)
      // 这次请求已经不是最新的了（用户又查了别的）：整个结果都作废，
      // 连下面新闻的几条状态也别动，否则会把新票的新闻覆盖/关掉
      if (seq !== runSeqRef.current) return
      setResult(found)
      // 新闻单独再发一次：它要出外网、慢且可能失败，别让它拖住结论；
      // 失败也不弹错误条 —— 那一块自己显示原因就够了
      setNews(null)
      setNewsLoading(true)
      newsCodeRef.current = found.code
      api
        .stockNews(found.code)
        .then((data) => {
          if (newsCodeRef.current === found.code) setNews(data)
        })
        .catch((err: Error) => {
          if (newsCodeRef.current !== found.code) return
          setNews({
            code: found.code,
            name: found.name,
            rows: [],
            hidden: 0,
            note: `新闻取不到：${err.message}`,
          })
        })
        .finally(() => {
          if (newsCodeRef.current === found.code) setNewsLoading(false)
        })
    } catch (err) {
      // 过期的那次请求失败了也不该弹错（用户已经在看新票了）
      if (seq !== runSeqRef.current) return
      // 解析不出 / 匹配到多只时后端给的就是一句能直接读的话，原样显示；
      // **不清空上一次的结果** —— 敲错一个字不该把刚查到的东西抹掉
      setError((err as Error).message)
    } finally {
      // 只有最新那次请求才负责关加载态，否则会把新票的「分析中…」提前关掉
      if (seq === runSeqRef.current) setLoading(false)
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
              分析里带技术 / 新闻 / 基本面三面<b className="font-normal text-fg-muted">并列参考</b>
              （各看各的，<b className="font-normal text-fg-muted">不做自动合议</b>）：
              技术面＝下面的结论卡，基本面＝行业与复盘关联，新闻＝东财那条源（每次分析现取；
              只列标题里出现这只票的，名单类稿件按条数标出、不混在里面）。
              <br />
              结论卡这套模板是<b className="font-normal text-fg-muted">按 20cm 强势票校准的</b>
              （创业板 / 科创板那种），随手查的普通票多半会显示「没动静 / 对不上」——
              那是模板不匹配，不等于这只票有问题。
            </p>
          </div>
        </Panel>

        {result && (
          <>
            <Panel
              // 票名／代码做成可点链接，直接跳个股页（2026-10-10 用户要求）。
              // 与 meta 里那条「看完整个股页」是同一个去处 —— 名字本来就最像可点的东西
              title={
                <Link
                  to={`/stock/${encodeURIComponent(result.code)}`}
                  title="打开个股页"
                  className="transition-colors hover:text-accent"
                >
                  {result.name ?? result.code}  {result.code}
                </Link>
              }
              meta={
                <span className="num text-[13px]">
                  {result.trade_date ?? '—'}
                  <span className="mx-2">·</span>
                  <span className={toneOf(result.pct_chg)}>{fmtPct(result.pct_chg)}</span>
                  <Link
                    to={`/stock/${encodeURIComponent(result.code)}`}
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
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="text-fg-dim">复盘关联</span>
                  {/* ⚠️ 这两个计数是**库里有数据的那些天**里、按**去重交易日**数的「天数」，
                      不是行数也不是历史累计 —— 必须把分母窗口一起显示，否则会被读成
                      「这只票一辈子涨停 12 次」。两张表窗口不一定一样，所以分开标。 */}
                  <span
                    className="num text-fg-muted"
                    title={`窗口＝库里已有数据的天数，不是历史累计（涨停池表覆盖 ${result.limit_up_days} 天、龙虎榜表覆盖 ${result.lhb_days} 天）`}
                  >
                    涨停 {result.limit_up_count} 天 · 龙虎榜 {result.lhb_count} 天
                  </span>
                  <span className="num text-[12px] text-fg-dim">
                    （窗口 {result.limit_up_days} / {result.lhb_days} 个交易日）
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
 * 只放行 http(s) 链接。来源（东财）的 url 字段没有清洗，别的协议（比如 `javascript:`）
 * 直接进 `<a href>` 是注入面 —— 不合法就不渲染成链接，标题照常显示。
 */
function isHttpUrl(url: string): boolean {
  return /^https?:\/\//i.test(url)
}

/**
 * 新闻那一块（「三面并列」里的新闻面）。
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
                {isHttpUrl(row.url) ? (
                  <a
                    href={row.url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-[13px] text-fg transition-colors hover:text-accent"
                  >
                    {row.title}
                  </a>
                ) : (
                  <span className="text-[13px] text-fg">{row.title}</span>
                )}
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
