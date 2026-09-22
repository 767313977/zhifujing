import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { LimitStock, PoolType } from '../api/types'
import { fmtAmount, fmtInt, fmtNum, fmtPct, fmtSealTime, toneOf } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs, SortValue } from '../lib/sort'
import { rememberStockList } from '../lib/stockNav'
import Panel from './Panel'
import SortTh from './SortTh'

interface LimitTableProps {
  type: PoolType
  stocks: LimitStock[]
  delay?: number
  /** 首屏取数期间为真。不加这个的话，空列表会被显示成「当日无数据」 */
  loading?: boolean
}

interface Column {
  key: string
  label: string
  /** 默认右对齐（数值列）；文字列显式左对齐 */
  align?: 'left' | 'right'
  render: (stock: LimitStock) => ReactNode
  /**
   * 取这一列的排序值。不给 = 这列没有可比值、表头不可点。
   *
   * 直接复用列的 `key` 当排序字段名，不再另起一套 key ——
   * 两套名字最容易出现「点 A 列排的是 B 列」。
   */
  sortValue?: (stock: LimitStock) => SortValue
}

const TITLES: Record<PoolType, string> = {
  up: '涨停明细',
  down: '跌停明细',
  broken: '炸板明细',
}

/**
 * 三池的列不完全一样（跌停池是「封单资金/连续跌停/末封」，炸板池没有封板资金），
 * 这里按类型装配列，而不是硬套同一套表头。
 *
 * `hasBoard` / `hasReason` 由调用方**按数据判**：开盘啦板块来自涨停天梯、涨停原因
 * 来自同花顺涨停池，两者都只覆盖涨停股，跌停 / 炸板池拿不到，那种情况下不给这两列
 * （留一整列「—」只是噪音）。
 */
function buildColumns(
  type: PoolType,
  { hasBoard = false, hasReason = false }: { hasBoard?: boolean; hasReason?: boolean } = {},
): Column[] {
  const columns: Column[] = [
    {
      key: 'code',
      label: '代码',
      align: 'left',
      // 代码和名称都是跳个股页（看日 K）的入口。为什么不把整行做成链接：
      // 行上已经有 onClick（记列表、供个股页 ← → 翻），再让 tr 也导航就会
      // 和 Link 各推一次历史 —— 退回来要按两下。Patterns 页记过这个坑。
      render: (s) => (
        <Link
          to={`/stock/${s.code}`}
          className="num text-fg-muted transition-colors hover:text-accent"
        >
          {s.code}
        </Link>
      ),
      sortValue: (s) => s.code,
    },
    {
      key: 'name',
      label: '名称',
      align: 'left',
      // 名称下面曾经挂过一层「题材标签」。开盘啦板块单开成列之后就撤了 ——
      // `stock_concept` 里每只票恰好一个板块，标签和那一列是同一个词，
      // 摆两遍只是重复（撤掉时的实测见设计文档 8.36.1）。
      render: (s) => (
        <Link
          to={`/stock/${s.code}`}
          className="text-fg transition-colors hover:text-accent"
        >
          {s.name ?? '—'}
        </Link>
      ),
      sortValue: (s) => s.name,
    },
    {
      key: 'industry',
      label: '行业',
      align: 'left',
      render: (s) => <span className="text-[12px] text-fg-dim">{s.industry ?? '—'}</span>,
      sortValue: (s) => s.industry,
    },
    /* 「开盘啦板块」与「行业」并存而不是替换：后者是 iFinD 的同花顺行业（公司做什么
       生意），前者是开盘啦精选板块（今天为什么涨停），两个问题不一样，都留着。

       **只在真有数据时给这一列**：它的来源是涨停天梯（只覆盖涨停股），跌停池与炸板池
       恒为空，留一整列「—」只是噪音 —— 与「没有题材数据时不给『名称 / 题材』表头」
       同一个做法。 */
    ...(hasBoard
      ? [
          {
            key: 'board',
            label: '开盘啦板块',
            align: 'left' as const,
            render: (s: LimitStock) => (
              <span className="text-[12px] text-fg-dim">{s.board ?? '—'}</span>
            ),
            sortValue: (s: LimitStock) => s.board,
          },
        ]
      : []),
    /* 涨停原因（同花顺 `reason_type`：「房地产+城市更新+北京国资」）。
       放在板块右边 —— 两个都是「为什么涨」，一个给板块视角、一个给原因串。
       这列**不给排序**：按一长串中文排序没有意义，表头也就不该点得动。
       长原因用 `truncate` 收窄（列宽不能被它撑爆），原文挂 title 里，悬停可看全。 */
    ...(hasReason
      ? [
          {
            key: 'reason',
            label: '涨停原因',
            align: 'left' as const,
            render: (s: LimitStock) => (
              <span
                className="block max-w-[240px] truncate text-[12px] text-fg-muted"
                title={s.reason ?? undefined}
              >
                {s.reason ?? '—'}
              </span>
            ),
          },
        ]
      : []),
    {
      key: 'pct',
      label: '涨跌幅',
      render: (s) => (
        <span className={`num ${toneOf(s.pct_chg)}`}>{fmtPct(s.pct_chg)}</span>
      ),
      sortValue: (s) => s.pct_chg,
    },
    {
      key: 'price',
      label: '最新价',
      render: (s) => <span className="num">{fmtNum(s.price, 2)}</span>,
      sortValue: (s) => s.price,
    },
  ]

  if (type === 'up') {
    columns.push({
      key: 'consecutive',
      label: '连板',
      render: (s) => (
        <span className={`num ${(s.consecutive ?? 1) >= 3 ? 'text-accent' : 'text-fg'}`}>
          {fmtInt(s.consecutive)}
        </span>
      ),
      sortValue: (s) => s.consecutive,
    })
  }

  if (type !== 'broken') {
    columns.push({
      key: 'seal_amount',
      label: type === 'up' ? '封板资金' : '封单资金',
      render: (s) => <span className="num">{fmtAmount(s.seal_amount)}</span>,
      sortValue: (s) => s.seal_amount,
    })
  }

  if (type === 'down') {
    columns.push({
      key: 'continuous',
      label: '连续跌停',
      render: (s) => <span className="num text-down">{fmtInt(s.consecutive)}</span>,
      sortValue: (s) => s.consecutive,
    })
    columns.push({
      key: 'last_seal',
      label: '末封',
      render: (s) => <span className="num text-fg-muted">{fmtSealTime(s.last_seal_time)}</span>,
      // 封板时间是 `09:31:00` 这种定长写法，按字符串排就等价于按时间排
      sortValue: (s) => s.last_seal_time,
    })
  } else {
    columns.push({
      key: 'first_seal',
      label: '首封',
      render: (s) => <span className="num text-fg-muted">{fmtSealTime(s.first_seal_time)}</span>,
      sortValue: (s) => s.first_seal_time,
    })
  }

  columns.push({
    key: 'open_times',
    label: type === 'down' ? '开板' : '炸板',
    render: (s) => (
      <span className={`num ${(s.open_times ?? 0) > 0 ? 'text-fg' : 'text-fg-dim'}`}>
        {fmtInt(s.open_times)}
      </span>
    ),
    sortValue: (s) => s.open_times,
  })
  columns.push({
    key: 'turnover',
    label: '换手率',
    render: (s) => <span className="num">{fmtNum(s.turnover, 2, '%')}</span>,
    sortValue: (s) => s.turnover,
  })
  columns.push({
    key: 'amount',
    label: '成交额',
    render: (s) => <span className="num text-fg-muted">{fmtAmount(s.amount)}</span>,
    sortValue: (s) => s.amount,
  })
  columns.push({
    key: 'float_mv',
    label: '流通市值',
    render: (s) => (
      // 总市值放悬停里而不是单开一列：涨停明细本来就有 12 列，
      // 再加一列在 1280~1500px 这些常用宽度上会挤到必须横向滚。
      // 打板主要看流通盘（决定封板难度），总市值只是「大票还是小票」的旁证
      <span
        className="num text-fg-muted"
        title={s.total_mv != null ? `总市值 ${fmtAmount(s.total_mv)}` : undefined}
      >
        {fmtAmount(s.float_mv)}
      </span>
    ),
    sortValue: (s) => s.float_mv,
  })

  return columns
}

/** 把列定义里的 sortValue 收集成排序口径。左对齐的按升序起手（代码、名称、行业） */
function buildSpecs(columns: Column[]): SortSpecs<LimitStock> {
  const specs: SortSpecs<LimitStock> = {}
  for (const column of columns) {
    if (column.sortValue) {
      specs[column.key] = {
        value: column.sortValue,
        first: column.align === 'left' ? 'asc' : 'desc',
      }
    }
  }
  return specs
}

export default function LimitTable({
  type,
  stocks,
  delay = 280,
  loading = false,
}: LimitTableProps) {
  const columns = buildColumns(type, {
    hasBoard: stocks.some((stock) => stock.board),
    hasReason: stocks.some((stock) => stock.reason),
  })
  // 首屏不排（key 为 null），保持后端顺序：涨停池的默认顺序本身有意义
  const [sort, shown] = useSort(stocks, buildSpecs(columns), { key: null })

  return (
    <Panel
      title={TITLES[type]}
      meta={
        <span className="num">
          {loading ? '加载中…' : `${stocks.length} 只`}
          {!loading && stocks.length > 0 && (
            <span className="ml-2 text-fg-dim">点名称看日 K · 点列头排序</span>
          )}
        </span>
      }
      delay={delay}
    >
      {loading ? (
        <div className="px-4 py-8 text-center text-[13px] text-fg-dim">加载中…</div>
      ) : stocks.length === 0 ? (
        <div className="px-4 py-8 text-center text-[13px] text-fg-dim">当日无数据</div>
      ) : (
        <div className="max-h-[440px] overflow-auto">
          <table className="grid-table">
            <thead>
              <tr>
                {columns.map((column) => (
                  <SortTh
                    key={column.key}
                    {...sort}
                    sortKey={column.sortValue ? column.key : undefined}
                    align={column.align}
                  >
                    {column.label}
                  </SortTh>
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map((stock) => (
                <tr
                  key={stock.code}
                  // 只记列表、不导航：跳转交给名称/代码那个 Link。
                  // 点击会从 Link 冒泡上来，所以点名称时这里也会执行
                  onClick={() => rememberStockList(shown.map((item) => item.code))}
                >
                  {columns.map((column) => (
                    <td
                      key={column.key}
                      className={column.align === 'left' ? '!text-left' : undefined}
                    >
                      {column.render(stock)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}
