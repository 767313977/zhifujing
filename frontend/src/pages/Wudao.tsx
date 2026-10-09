import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { PatternStock, PatternSummary } from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import StockLink from '../components/StockLink'
import { fmtNum, fmtPct, toneOf } from '../lib/format'
import { rememberStockList } from '../lib/stockNav'

/**
 * 悟道之路（移植自 zhaohuibin7/yangban-desk 的选股逻辑）。
 *
 * 这个页面就是它的三张选股页在站内的落点 —— 判定逻辑已进形态引擎
 * （见 `services/patterns.py` 里 `wudao_*` / `pile_wash_*` / `huabao_early`），
 * 这里只负责按它原来的分页把**当天的名单**摆出来。
 *
 * ⚠️ 形态的判定数字照抄它的源码（2026-10-08，移植方案见
 * `docs/plans/2026-10-08-yangban-port.md`），所以**改这里的文案前先对一下那边**：
 * 池子标题、关键位列、纪律说明都是从它的 `PHASE_DO` / `plan_text` 抄过来的。
 * ⚠️ 回测四个池子都是「短周期略有指向、胜率不到 50%」→ 当清单用，不是荐股
 * （它的 License 也写着「勿宣传为荐股或收益承诺」）。
 */
interface Pool {
  key: string
  title: string
  /** 关键位列：`[key_levels 里的字段, 列名]` */
  levels: [string, string][]
}

interface Section {
  page: string
  note: string
  pools: Pool[]
}

/** 按它的三个页面分块，块内顺序也照它（早期池排在主逻辑之后）。 */
const SECTIONS: Section[] = [
  {
    page: '辉宾选股',
    note:
      '主交易纪律：样板 → 启动。样板日是观察日、不是追高日；买点是启动日「盘中刚过昨高」那一下，' +
      '收盘贴板才看见的默认不追。华宝早期是更早一档，只观察。',
    pools: [
      { key: 'wudao_sample', title: '明天盯', levels: [['watch_high', '今高']] },
      { key: 'wudao_start', title: '今天可买', levels: [['breakout', '昨高']] },
      { key: 'huabao_early', title: '华宝早期', levels: [['ma5', 'MA5']] },
    ],
  },
  {
    page: '辉宾选股2',
    note:
      '启动之后今天在洗盘消化的票：明天盘中放量过「洗盘高」再小仓，破启动低作废；' +
      '缩量阴过洗高不算。今天是洗盘日 → 只观察记账，不追尖、不加仓。',
    pools: [
      {
        key: 'wudao_wash2',
        title: '洗完可盯',
        levels: [
          ['watch_high', '洗高'],
          ['start_low', '作废'],
        ],
      },
    ],
  },
  {
    page: '黑白选股',
    note:
      '多日结构：堆量吸筹 → 放量拉升 → 缩量洗盘。「缩量洗盘中」是主观察（记下洗盘高、缩量别乱砍），' +
      '「洗后可盯」才是可小仓试的那一档。已经主升完 / 回撤吐光的不在名单里。',
    pools: [
      {
        key: 'pile_wash_ready',
        title: '洗后可盯',
        levels: [
          ['breakout', '洗盘高'],
          ['wash_low', '作废'],
        ],
      },
      { key: 'pile_wash_wash', title: '缩量洗盘中', levels: [['wash_high', '洗盘高']] },
    ],
  },
]

const POOL_LIMIT = 3000

/**
 * 每张表最多渲染多少行。
 *
 * 「缩量洗盘中」「华宝早期」这类是全池扫出来的，一天几百只是常态（10-08：633 / 190），
 * 全塞进 DOM 既卡又没人翻 —— 按分数降序只渲染前这么多，**总数照旧显示在标题上**，
 * 并在表尾写明「还有多少只没显示」。想看得更多就调大这个数（数据本身是全的，
 * `POOL_LIMIT` 才是真正的取数上限）。
 */
const RENDER_LIMIT = 100

/** 形态 key → 池子标题，用来标「这只票也命中了别的池子」。 */
const POOL_TITLES: Record<string, string> = Object.fromEntries(
  SECTIONS.flatMap((section) => section.pools.map((pool) => [pool.key, pool.title])),
)

export default function Wudao() {
  const [summary, setSummary] = useState<PatternSummary | null>(null)
  const [hits, setHits] = useState<Record<string, PatternStock[]>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const pools = useMemo(() => SECTIONS.flatMap((section) => section.pools), [])

  const reload = useCallback(() => {
    setLoading(true)
    setError(null)
    Promise.all([
      api.patternSummary(null),
      ...pools.map((pool) =>
        api.patternHits(null, 0, POOL_LIMIT, pool.key).then((rows) => [pool.key, rows] as const),
      ),
    ])
      .then(([sum, ...rows]) => {
        setSummary(sum)
        setHits(Object.fromEntries(rows))
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false))
  }, [pools])

  useEffect(() => {
    reload()
  }, [reload])

  const countOf = useMemo(() => {
    const map: Record<string, number> = {}
    for (const item of summary?.by_pattern ?? []) map[item.pattern] = item.stocks
    return map
  }, [summary])

  return (
    <Layout
      toolbar={
        <>
          <span className="num text-[13px] text-fg-dim">
            {summary?.trade_date ? `${summary.trade_date} 收盘后扫描` : '—'}
          </span>
          <button
            type="button"
            onClick={reload}
            className="shrink-0 border border-line px-2.5 py-1 text-[13px] text-fg-muted transition-colors hover:border-line-soft hover:text-fg"
          >
            刷新
          </button>
        </>
      }
    >
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}

      <div className="space-y-4">
        <Panel
          title="悟道之路"
          meta={<span className="num">移植自 yangban-desk · 六个池子</span>}
          delay={30}
        >
          <div className="space-y-2 px-4 py-3.5 text-[13px] leading-relaxed text-fg-muted">
            <p>
              判定逻辑 2026-10-08 从{' '}
              <a
                href="https://github.com/zhaohuibin7/yangban-desk"
                target="_blank"
                rel="noreferrer"
                className="text-fg-dim underline decoration-line underline-offset-2 hover:text-fg"
              >
                zhaohuibin7/yangban-desk
              </a>{' '}
              移植进本站形态引擎（数字逐条照抄源码），这个页面按它原来的三张选股页摆出当天名单。
            </p>
            <p>
              清单的<b className="font-normal text-fg-muted">扫描范围按形态各定</b>（2026-10-08 优化）：
              「明天盯 / 今天可买」只看当天<b className="font-normal text-fg-muted">创业板 + 沪深主板里冲高过 4.5% 的票</b>
              （几十到一两百只，用的就是判定要的那份日线，不再依赖行情快照）；
              「洗完可盯」「华宝早期」「黑白选股」<b className="font-normal text-fg-muted">不限池子、扫全市场</b> —— 它们的票今天往往很安静
              （洗盘、连阳初期涨幅只有 1~3%），拿「今天强势」当候选等于把它们全筛掉。
            </p>
            <p className="text-fg-dim">
              ⚠️ 四个池子的回测都是「短周期略有指向、胜率不到 50%、四档中位数全负」——
              <b className="font-normal text-fg-muted">当清单看，不是买点信号</b>；
              量比口径一律是「当日成交量 ÷ 近 20 日均量」，不与昨日比。
            </p>
          </div>
        </Panel>

        {SECTIONS.map((section) => (
          <div key={section.page} className="space-y-4">
            <div className="border-b border-line-soft pb-1.5">
              <span className="text-[14px] text-fg">{section.page}</span>
              <span className="ml-2 text-[12px] leading-relaxed text-fg-dim">{section.note}</span>
            </div>
            {section.pools.map((pool) => (
              <PoolPanel
                key={pool.key}
                pool={pool}
                rows={hits[pool.key] ?? []}
                loading={loading}
                count={countOf[pool.key]}
              />
            ))}
          </div>
        ))}
      </div>
    </Layout>
  )
}

function PoolPanel({
  pool,
  rows,
  loading,
  count,
}: {
  pool: Pool
  rows: PatternStock[]
  loading: boolean
  count?: number
}) {
  // 一只票在本池里的分数与关键位要看**它自己**那条命中记录（同一只票可能命中别的形态），
  // 顺便把「它还命中了哪些池子」也算出来 —— 同一只票常同时是样板 + 洗盘，不该重复研究。
  const pick = useMemo(
    () =>
      rows.map((row) => ({
        row,
        hit: row.patterns.find((item) => item.pattern === pool.key),
        others: row.patterns
          .filter((item) => item.pattern !== pool.key && POOL_TITLES[item.pattern])
          .map((item) => POOL_TITLES[item.pattern]),
      })),
    [rows, pool.key],
  )
  // 分数降序（后端已排过），但只渲染前 RENDER_LIMIT 行 —— 见那个常量的注释
  const shown = useMemo(() => pick.slice(0, RENDER_LIMIT), [pick])
  const hidden = pick.length - shown.length

  return (
    <Panel
      title={pool.title}
      meta={
        <span className="num">
          {loading ? '加载中…' : `${(count ?? rows.length)} 只`}
          <span className="ml-2 text-fg-dim">按分数降序</span>
        </span>
      }
      delay={60}
    >
      {loading ? (
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">加载中…</div>
      ) : rows.length === 0 ? (
        <div className="px-4 py-8 text-center text-[14px] text-fg-dim">当日无命中</div>
      ) : (
        <>
          <div className="max-h-[520px] overflow-auto">
            <table className="grid-table">
              <thead>
                <tr>
                  <th>代码</th>
                  <th className="!text-left">名称</th>
                  <th className="!text-left">板块</th>
                  <th>收盘</th>
                  <th>涨跌幅</th>
                  <th>分数</th>
                  {pool.levels.map(([field, label]) => (
                    <th key={field}>{label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {shown.map(({ row, hit, others }) => (
                  <tr
                    key={row.code}
                    // 记整份名单给个股页 ← → 前后翻（StockLink 只管导航，靠冒泡触发这里）
                    onClick={() => rememberStockList(rows.map((item) => item.code))}
                    title={[row.name ?? '', row.industry ?? '', row.sectors.join('、')]
                      .filter(Boolean)
                      .join(' · ')}
                  >
                    <td>
                      <StockLink code={row.code} className="num text-fg-muted">
                        {row.code}
                      </StockLink>
                    </td>
                    <td className="!text-left">
                      <StockLink code={row.code}>{row.name ?? row.code}</StockLink>
                      {others.length > 0 && (
                        <span className="ml-2 text-[12px] text-fg-dim">
                          也是{others.join('/')}
                        </span>
                      )}
                    </td>
                    <td className="!text-left">
                      <span className="text-[13px] text-fg-muted">
                        {row.sectors.length > 0 ? row.sectors.join('、') : '—'}
                      </span>
                    </td>
                    <td>
                      <span className="num">{fmtNum(row.close, 2)}</span>
                    </td>
                    <td>
                      <span className={`num ${toneOf(row.pct_chg)}`}>{fmtPct(row.pct_chg)}</span>
                    </td>
                    <td>
                      <span className="num text-fg">{fmtNum(hit?.score ?? row.score, 1)}</span>
                    </td>
                    {pool.levels.map(([field]) => (
                      <td key={field}>
                        <span className="num text-fg-muted">
                          {fmtNum(hit?.key_levels?.[field] ?? null, 2)}
                        </span>
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {hidden > 0 && (
            <div className="border-t border-line-soft px-4 py-2 text-[12px] text-fg-dim">
              按分数只显示前 {RENDER_LIMIT} 只，另有 {hidden} 只没显示
            </div>
          )}
        </>
      )}
    </Panel>
  )
}
