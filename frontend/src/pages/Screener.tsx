import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Preset, ScreenRun } from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import SortTh from '../components/SortTh'
import StockLink from '../components/StockLink'
import { fmtAmount } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'

/** 示例条件：自然语言选股的能力边界不直观，给几个能直接用的例子最省事。 */
const EXAMPLES = [
  '电子行业市值大于500亿的今日涨幅大于3%的股票',
  '连续3天涨停的股票',
  '市盈率低于15且股息率大于3%的银行股',
  '今日换手率大于10%且涨幅大于5%的股票',
]

/** 文本列左对齐，数值列右对齐。 */
const TEXT_COLUMNS = ['代码', '简称', '名称', '行业', '分类']

/**
 * 「代码」「名称」两列要能点进个股页看日K，其余列是纯数据。
 *
 * 列名由 iFinD 按提问内容给出，实测有 `股票代码 / 证券代码 / 股票简称 /
 * 证券简称` 几种写法，所以按后缀匹配而不是写死整名。
 */
function isCodeColumn(name: string): boolean {
  return name.endsWith('代码')
}

function isNameColumn(name: string): boolean {
  return name.endsWith('简称') || name === '名称'
}

function isTextColumn(name: string): boolean {
  return TEXT_COLUMNS.some((token) => name.includes(token))
}

/**
 * 动态列的排序口径。列由 iFinD 按提问内容决定，所以只能现构造。
 *
 * iFinD 的值一律是字符串（还有 `2.2530271027878E11` 这种科学计数法），
 * 所以**能解析成数字的按数字排，否则按文字排**。同一列里混着数字与文字时
 * `sortRows` 会退化成字符串比较 —— 不会崩，只是那几行按字典序排。
 */
function buildSpecs(columns: string[]): SortSpecs<Record<string, string>> {
  const specs: SortSpecs<Record<string, string>> = {}
  for (const column of columns) {
    const text = isTextColumn(splitColumn(column).name)
    specs[column] = {
      value: (row) => toNumber(row[column]) ?? row[column],
      first: text ? 'asc' : 'desc',
    }
  }
  return specs
}

function splitColumn(column: string): { name: string; note?: string } {
  // iFinD 的列名自带日期，如「总市值[20260917]」，拆出来单独弱化显示
  const matched = column.match(/^(.*?)\[(\d{8})\]$/)
  if (!matched) return { name: column }
  const [, name, day] = matched
  return { name, note: `${day.slice(4, 6)}-${day.slice(6, 8)}` }
}

function toNumber(raw: string | undefined): number | null {
  if (raw == null || raw === '') return null
  const value = Number(raw)
  return Number.isFinite(value) ? value : null
}

interface Cell {
  text: string
  tone?: string
}

/**
 * iFinD 返回值是原始字符串（含科学计数法如 2.2530271027878E11），
 * 按列名判断语义后再格式化，否则数字完全不可读。
 */
function formatCell(column: string, raw: string | undefined): Cell {
  const { name } = splitColumn(column)
  const text = raw ?? ''
  const value = toNumber(text)

  const isPercent =
    name.includes('涨跌幅') ||
    name.includes('涨幅') ||
    name.includes('跌幅') ||
    name.includes('换手率') ||
    name.includes('股息率') ||
    name.includes('占比')
  if (isPercent && value != null) {
    const sign = name.includes('涨跌幅') || name.includes('涨幅') ? (value >= 0 ? '+' : '') : ''
    return {
      text: `${sign}${value.toFixed(2)}%`,
      tone: value > 0 ? 'text-up' : value < 0 ? 'text-down' : 'text-fg-muted',
    }
  }

  if ((name.includes('市值') || name.includes('成交额') || name.includes('金额')) && value != null) {
    return { text: fmtAmount(value) }
  }

  if (name.includes('涨停') || name.includes('跌停')) {
    // 布尔型指标，iFinD 用 1/0 或 是/否 表达
    if (text === '1' || text === '是' || text.toLowerCase() === 'true') {
      return { text: '✓', tone: 'text-up' }
    }
    if (text === '0' || text === '否' || text.toLowerCase() === 'false') {
      return { text: '—', tone: 'text-fg-dim' }
    }
  }

  if (value != null && Math.abs(value) >= 1000) {
    return { text: value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) }
  }
  if (value != null && !Number.isInteger(value)) {
    return { text: value.toFixed(2) }
  }
  return { text: text || '—' }
}

export default function Screener() {
  const [query, setQuery] = useState(EXAMPLES[0])
  const [presetName, setPresetName] = useState('')
  const [result, setResult] = useState<ScreenRun | null>(null)
  const [presets, setPresets] = useState<Preset[]>([])
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [watched, setWatched] = useState<Set<string>>(new Set())

  const reloadPresets = useCallback(() => {
    api.presets().then(setPresets).catch(() => setPresets([]))
  }, [])

  useEffect(() => {
    reloadPresets()
    api
      .watchlist()
      .then((rows) => setWatched(new Set(rows.map((row) => row.code))))
      .catch(() => setWatched(new Set()))
  }, [reloadPresets])

  const runScreen = useCallback(async (text: string) => {
    const trimmed = text.trim()
    if (trimmed.length < 2) {
      setError('请输入至少 2 个字的选股条件')
      return
    }
    setRunning(true)
    setError(null)
    setNotice(null)
    try {
      setResult(await api.runScreen(trimmed))
    } catch (err) {
      setResult(null)
      setError((err as Error).message)
    } finally {
      setRunning(false)
    }
  }, [])

  const savePreset = useCallback(async () => {
    const name = presetName.trim() || query.trim().slice(0, 20)
    if (!name) {
      setError('请先填写条件，或给预设起个名字')
      return
    }
    try {
      await api.savePreset(name, query.trim())
      setPresetName('')
      setNotice(`已保存条件「${name}」`)
      reloadPresets()
    } catch (err) {
      setError((err as Error).message)
    }
  }, [presetName, query, reloadPresets])

  const addToWatchlist = useCallback(
    async (code: string, name: string) => {
      try {
        await api.addWatchlist(code, name)
        setWatched((prev) => new Set(prev).add(code.replace(/\..*$/, '')))
        setNotice(`已加入自选：${name}`)
      } catch (err) {
        setError((err as Error).message)
      }
    },
    [],
  )

  const toolbar = (
    <span className="num hidden text-[12px] text-fg-dim lg:inline">
      {running ? '选股中…' : result ? `${result.returned} 行结果` : '就绪'}
    </span>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && (
        <Alert onClose={() => setError(null)}>{error}</Alert>
      )}
      {notice && <Alert tone="accent" onClose={() => setNotice(null)}>{notice}</Alert>}

      <div className="space-y-4">
        <Panel
          title="自然语言选股"
          meta={<span className="num">iFinD 智能选股 · 用一句话描述条件</span>}
          delay={40}
        >
          <div className="space-y-3 px-4 py-3.5">
            <div className="flex flex-col gap-2 sm:flex-row">
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') void runScreen(query)
                }}
                placeholder="例如：电子行业市值大于500亿且今日涨幅大于3%的股票"
                className="min-w-0 flex-1 border border-line bg-ink-850 px-3 py-2 text-[13px] text-fg outline-none placeholder:text-fg-dim focus:border-fg-dim"
              />
              <button
                type="button"
                onClick={() => void runScreen(query)}
                disabled={running}
                className="shrink-0 border border-accent/60 bg-accent/10 px-5 py-2 text-[13px] text-accent transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {running ? '选股中…' : '选股'}
              </button>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[12px] tracking-[0.1em] text-fg-dim">示例</span>
              {EXAMPLES.map((example) => (
                <button
                  key={example}
                  type="button"
                  onClick={() => setQuery(example)}
                  title={example}
                  className="max-w-[280px] truncate border border-line-soft px-2 py-1 text-[12px] text-fg-muted transition-colors hover:border-line hover:text-fg"
                >
                  {example}
                </button>
              ))}
            </div>

            <div className="flex flex-col gap-2 border-t border-line-soft pt-3 sm:flex-row sm:items-center">
              <input
                value={presetName}
                onChange={(event) => setPresetName(event.target.value)}
                placeholder="给这个条件起个名字（留空则自动截取）"
                className="min-w-0 flex-1 border border-line bg-ink-850 px-3 py-1.5 text-[12px] text-fg outline-none placeholder:text-fg-dim focus:border-fg-dim"
              />
              <button
                type="button"
                onClick={() => void savePreset()}
                className="shrink-0 border border-line px-3 py-1.5 text-[12px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg"
              >
                保存条件
              </button>
            </div>
          </div>
        </Panel>

        <Panel
          title="保存的条件"
          meta={
            <span className="num">
              {presets.length > 0 ? `${presets.length} 条 · 同名保存会覆盖` : '—'}
            </span>
          }
          delay={80}
        >
          {presets.length === 0 ? (
            <div className="px-4 py-6 text-center text-[12px] text-fg-dim">
              还没有保存的条件。写一条问句，点上面的「保存条件」就能一键复用
            </div>
          ) : (
            <div className="divide-y divide-line-soft">
              {presets.map((preset) => (
                <div key={preset.id} className="flex items-center gap-3 px-4 py-2.5">
                  <span className="w-28 shrink-0 truncate text-[13px] text-fg" title={preset.name}>
                    {preset.name}
                  </span>
                  <span
                    className="min-w-0 flex-1 truncate text-[12px] text-fg-dim"
                    title={String(preset.conditions.query ?? '')}
                  >
                    {String(preset.conditions.query ?? '')}
                  </span>
                  <button
                    type="button"
                    onClick={() => {
                      const text = String(preset.conditions.query ?? '')
                      setQuery(text)
                      void runScreen(text)
                    }}
                    className="shrink-0 border border-line px-2.5 py-1 text-[12px] text-fg-muted transition-colors hover:border-accent/50 hover:text-accent"
                  >
                    执行
                  </button>
                  <button
                    type="button"
                    onClick={async () => {
                      // 删除失败必须说出来：静默失败会让人以为删掉了，下次刷新它
                      // 又冒出来；而且不 catch 会抛出一个没人接的 promise
                      try {
                        await api.deletePreset(preset.id)
                        reloadPresets()
                      } catch (err) {
                        setError((err as Error).message)
                      }
                    }}
                    className="shrink-0 border border-line px-2.5 py-1 text-[12px] text-fg-dim transition-colors hover:border-danger/50 hover:text-danger"
                  >
                    删除
                  </button>
                </div>
              ))}
            </div>
          )}
        </Panel>

        <Panel
          title="选股结果"
          meta={
            result ? (
              <span className="num">
                {result.query}
                <span className="ml-3 text-fg-dim">{result.cost_seconds}s</span>
              </span>
            ) : (
              <span className="num">尚未执行</span>
            )
          }
          delay={120}
        >
          {!result ? (
            <div className="px-4 py-10 text-center text-[13px] text-fg-dim">
              输入条件后点「选股」，或直接点上面的示例
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-stretch gap-px border-b border-line-soft bg-line-soft">
                <div className="flex-1 bg-ink-900 px-4 py-2.5">
                  <div className="text-[12px] tracking-[0.1em] text-fg-dim">匹配总数</div>
                  <div className="num mt-1 text-[20px] leading-tight font-medium text-fg">
                    {result.matched ?? '—'}
                    <span className="ml-1 text-[12px] text-fg-dim">只</span>
                  </div>
                </div>
                <div className="flex-1 bg-ink-900 px-4 py-2.5">
                  <div className="text-[12px] tracking-[0.1em] text-fg-dim">表格返回</div>
                  <div className="num mt-1 text-[20px] leading-tight font-medium text-fg">
                    {result.returned}
                    <span className="ml-1 text-[12px] text-fg-dim">行</span>
                  </div>
                </div>
                <div className="flex-[2] bg-ink-900 px-4 py-2.5">
                  <div className="text-[12px] tracking-[0.1em] text-fg-dim">说明</div>
                  <div className="mt-1 text-[12px] leading-relaxed text-fg-muted">
                    列由 iFinD 按提问内容动态决定，列名方括号内是数据日期
                  </div>
                </div>
              </div>

              {/* 截断必须显眼：否则会把「返回 100 行」误读成「只有 100 只符合」 */}
              {result.truncated && (
                <div className="flex items-start gap-2.5 border-b border-accent/25 bg-accent/[0.05] px-4 py-2.5 text-[12px] leading-relaxed text-fg-muted">
                  <span className="mt-[3px] h-[6px] w-[6px] shrink-0 bg-accent" />
                  <span>
                    <span className="font-medium text-accent">结果被截断：</span>
                    实际匹配
                    <span className="num text-accent"> {result.matched} </span>
                    只，但数据源每张表格最多只给
                    <span className="num text-fg"> 100 </span>
                    行。要拿全建议在条件里加排名限制或收窄范围，例如「…按总市值排名前 50」。
                  </span>
                </div>
              )}

              {result.rows.length === 0 ? (
                <>
                  <div className="px-4 py-6 text-center text-[13px] text-fg-dim">
                    没有符合条件的股票，试试放宽条件
                  </div>
                  {/* **只在没结果时才展示数据源的回答**。成功时它是一整张结果表的
                      markdown 复本（实测 4279 字，与下面的表格逐行重复），只有
                      「0 只」时它才是有价值的那段：它会区分「条件太严」与
                      「这句话压根没被解析出来（有歧义）」。限高 + 可滚 */}
                  {result.answer && (
                    <div className="max-h-40 overflow-auto border-t border-line-soft bg-ink-850/40 px-4 py-2.5">
                      <div className="mb-1 text-[12px] tracking-[0.1em] text-fg-dim">
                        数据源的回答
                      </div>
                      <div className="whitespace-pre-wrap text-[12px] leading-relaxed text-fg-muted">
                        {result.answer}
                      </div>
                    </div>
                  )}
                </>
              ) : (
                <ResultTable result={result} watched={watched} onAdd={addToWatchlist} />
              )}
            </>
          )}
        </Panel>
      </div>
    </Layout>
  )
}

/**
 * 选股结果表。
 *
 * 单独抽出来是为了能调 `useSort` —— 结果为空/未选股时上面那块是条件渲染的，
 * hook 不能写在条件分支里。
 */
function ResultTable({
  result,
  watched,
  onAdd,
}: {
  result: ScreenRun
  watched: Set<string>
  onAdd: (code: string, name: string) => void
}) {
  // 首屏不排，保持数据源给的顺序（通常已经是有意义的排名）；点列头才排。
  // ⚠️ 表格最多 100 行，排的只是这 100 行 —— 截断提示就在上方
  const [sort, shown] = useSort(result.rows, buildSpecs(result.columns), { key: null })

  return (
    <div className="max-h-[560px] overflow-auto">
      <table className="grid-table">
        <thead>
          <tr>
            {result.columns.map((column) => {
              const { name, note } = splitColumn(column)
              return (
                <SortTh
                  key={column}
                  {...sort}
                  sortKey={column}
                  align={isTextColumn(name) ? 'left' : undefined}
                >
                  {/* 表头底色本身就是 fg-dim，所以列名要提亮一档
                      才能和后面的日期拉开对比 */}
                  <span className="text-fg-muted">{name}</span>
                  {note && <span className="ml-1">{note}</span>}
                </SortTh>
              )
            })}
            {/* 「操作」列没有可比的值，保持普通表头 */}
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((row, index) => {
            const code = (row['股票代码'] ?? row['证券代码'] ?? '').trim()
            const shortCode = code.replace(/\..*$/, '')
            const name = row['股票简称'] ?? row['证券简称'] ?? shortCode
            const isWatched = watched.has(shortCode)
            return (
              <tr key={`${code}-${index}`}>
                {result.columns.map((column) => {
                  const { name: columnName } = splitColumn(column)
                  const cell = formatCell(column, row[column])
                  // 代码 / 名称两列跳个股页。shortCode 为空（数据源没给代码）时
                  // 退回纯文本，免得点出一个 /stock/ 的空路径
                  const linkable =
                    shortCode !== '' &&
                    (isCodeColumn(columnName) || isNameColumn(columnName))
                  return (
                    <td
                      key={column}
                      className={isTextColumn(columnName) ? '!text-left' : undefined}
                    >
                      {linkable ? (
                        <StockLink
                          code={shortCode}
                          className={`num ${cell.tone ?? 'text-fg'}`}
                        >
                          {cell.text}
                        </StockLink>
                      ) : (
                        <span className={`num ${cell.tone ?? 'text-fg'}`}>{cell.text}</span>
                      )}
                    </td>
                  )
                })}
                <td>
                  <button
                    type="button"
                    disabled={isWatched || !shortCode}
                    onClick={() => onAdd(code, name)}
                    className="num border border-line px-2 py-[2px] text-[12px] text-fg-muted transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-default disabled:border-line-soft disabled:text-fg-dim"
                  >
                    {isWatched ? '已在自选' : '＋自选'}
                  </button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
