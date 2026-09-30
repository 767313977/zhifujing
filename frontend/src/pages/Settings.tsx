import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { AdminStatus, CollectLog, IfindToolUsage, TableCoverage } from '../api/types'
import Alert from '../components/Alert'
import InvitePanel from '../components/InvitePanel'
import Layout from '../components/Layout'
import MembersPanel from '../components/MembersPanel'
import Panel from '../components/Panel'
import SortTh from '../components/SortTh'
import { fmtDateTime, fmtInt, fmtShortDate } from '../lib/format'
import { useSort } from '../lib/sort'
import type { SortSpecs } from '../lib/sort'

/** 数据覆盖表：按列名 / 覆盖天数 / 最新日期排，用来找「哪张表历史最短」 */
const COVERAGE_SORTS: SortSpecs<TableCoverage> = {
  label: { value: (row) => row.label, first: 'asc' },
  days: { value: (row) => row.days },
  latest: { value: (row) => row.latest, first: 'asc' },
}

/** 调用次数是唯一有价值的排序目标：找出配额花在哪个工具上 */
const USAGE_SORTS: SortSpecs<IfindToolUsage> = {
  server: { value: (row) => row.server, first: 'asc' },
  tool: { value: (row) => row.tool, first: 'asc' },
  calls: { value: (row) => row.calls },
}

/** 排查采集问题时按耗时/行数排最有用；时间列排的是 `09-18 15:05` 这种定长写法 */
const LOG_SORTS: SortSpecs<CollectLog> = {
  created_at: { value: (log) => log.created_at },
  trade_date: { value: (log) => log.trade_date },
  task: { value: (log) => log.task, first: 'asc' },
  status: { value: (log) => log.status, first: 'asc' },
  rows: { value: (log) => log.rows },
  cost_seconds: { value: (log) => log.cost_seconds },
}

/** `Date` → 本地 `YYYY-MM-DD`。
 *
 * ⚠️ 不用 `toISOString().slice(0,10)`：那是按 **UTC** 切的，东八区凌晨 0~8 点
 * 会被算成**前一天**（2026-09-27 修）。
 */
function localDate(value: Date): string {
  const pad = (number: number) => String(number).padStart(2, '0')
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`
}

function sixMonthsAgo(): string {
  const now = new Date()
  const year = now.getFullYear()
  const month = now.getMonth() - 6
  // 目标月的天数上限：3/31 往前 6 个月应当是 9/30，而 `setMonth` **不做月末钳制**，
  // 会溢出成 10/1，默认起始日就差了一天（2026-09-27 修）。
  // `day = 0` 即上个月的最后一天，用它把日号夹回去。
  const lastDay = new Date(year, month + 1, 0).getDate()
  return localDate(new Date(year, month, Math.min(now.getDate(), lastDay)))
}

const LOG_TONE: Record<string, string> = {
  ok: 'text-fg',
  skipped: 'text-fg-dim',
  // 用 danger 而非 down：down 在本站是绿色（红涨绿跌），拿来表示失败会读反
  failed: 'text-danger',
}

export default function Settings() {
  const [status, setStatus] = useState<AdminStatus | null>(null)
  const [backfillStart, setBackfillStart] = useState(sixMonthsAgo)
  // 留空表示补到最近交易日（后端 end 参数缺省就是这个语义）
  const [backfillEnd, setBackfillEnd] = useState('')
  const [busy, setBusy] = useState<'collect' | 'backfill' | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // 三张表的排序状态都在这里统一声明：表格本身是条件渲染的
  // （数据没到就整块不出现），hook 不能写在条件分支里。
  // 首屏一律不排，保持后端顺序
  const [usageSort, usageRows] = useSort(
    status?.ifind_quota?.by_tool ?? [],
    USAGE_SORTS,
    { key: null },
  )
  const [coverageSort, coverageRows] = useSort(status?.coverage ?? [], COVERAGE_SORTS, {
    key: null,
  })
  const [logSort, logRows] = useSort(status?.recent_logs ?? [], LOG_SORTS, { key: null })

  const reload = useCallback(() => {
    api
      .adminStatus()
      .then(setStatus)
      .catch((err: Error) => setError(err.message))
  }, [])

  useEffect(() => {
    reload()
  }, [reload])

  const runBackfill = useCallback(async () => {
    setBusy('backfill')
    setError(null)
    setMessage('回补中，逐日补数可能持续几分钟，请勿关闭页面…')
    try {
      const result = await api.backfill(backfillStart, backfillEnd || undefined)
      setMessage(
        `回补完成：处理 ${result.days} 个交易日，失败步骤 ${result.failed_steps} 个`,
      )
      reload()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(null)
    }
  }, [backfillStart, backfillEnd, reload])

  const scheduler = status?.scheduler
  const quota = status?.ifind_quota
  const ratio = quota?.usage_ratio ?? null
  const usedPercent = ratio == null ? 0 : Math.round(ratio * 100)
  // 已用但不足 1% 时给最小可见宽度：否则 1/5000 这种刚起步的量会把填充块
  // 算成 0%，进度条看着像根本没渲染出来
  const barPercent =
    usedPercent === 0 && (quota?.cycle_calls ?? 0) > 0
      ? 1
      : Math.min(100, usedPercent)

  const toolbar = (
    <button
      type="button"
      onClick={reload}
      className="num border border-line px-2.5 py-[3px] text-[13px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg"
    >
      刷新
    </button>
  )

  return (
    <Layout toolbar={toolbar}>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}
      {message && (
        <Alert tone="accent" onClose={() => setMessage(null)}>
          {message}
        </Alert>
      )}

      <div className="space-y-4">
        <Panel
          title="定时任务"
          meta={
            <span className="num">
              {scheduler?.enabled ? '交易日收盘后自动采集' : '未启用'}
            </span>
          }
          delay={40}
        >
          <div className="grid grid-cols-2 overflow-hidden md:grid-cols-4">
            <Cell
              label="运行状态"
              value={scheduler?.running ? '运行中' : '已停止'}
              tone={scheduler?.running ? 'text-ok' : 'text-fg-dim'}
              pulse={scheduler?.running}
            />
            <Cell label="采集时刻" value={scheduler?.collect_time ?? '—'} />
            <Cell
              label="下次运行"
              value={fmtDateTime(scheduler?.next_run_time)}
              tone="text-accent"
            />
            <Cell label="上次运行" value={fmtDateTime(scheduler?.last_run)} />
          </div>
          <div className="border-t border-line-soft px-4 py-2.5 text-[13px] leading-relaxed text-fg-dim">
            采集时刻 {scheduler?.collect_time ?? '—'} 取自后端配置。
            {/* 时刻不写死在这里：它由后端 collect_hour/collect_minute 决定，
                写死的话改配置就会让这段说明悄悄变成错的（已经错过一次）。
                ⚠️ 2026-09-28 那次是**把「收盘后再跑一趟」整段删掉**，结果龙虎榜与机构席位
                从那天起再没入过库（这几类是「按日期问」的、没有回看窗口）——
                2026-09-30 补回来了，见 config.late_collect_hour。别再把这段删掉。 */}
            <span className="mx-1">·</span>
            收盘后 {scheduler?.late_collect_time ?? '—'} 还有一趟补采（
            {scheduler?.late_retry_time ?? '—'} 再兜底跑一次，同一趟代码、幂等重跑），只跑
            <b className="font-normal text-fg-muted">龙虎榜 / 机构席位 / 涨停题材 / 两融 / 北向</b>
            —— 这几类要收盘后（甚至晚上）才发布，15:05 那趟取不到（涨停题材给的是<b
              className="font-normal text-fg-muted"
            >上一交易日</b>的天梯），所以首页龙虎榜面板
            当天显示的多半是<b className="font-normal text-fg-muted">上一个交易日</b>
            的数据（面板上标了是哪天）。
            {scheduler?.catchup_on_start && (
              <>
                <span className="mx-1">·</span>
                已开启<b className="font-normal text-fg-muted">启动补采</b>：本机不常开，
                错过采集时刻后下次启动会在后台补上（两个时刻都会补）。
              </>
            )}
            <span className="mx-1">·</span>
            <b className="font-normal text-fg-muted">没有手动采集入口</b>
            ：采集只在上面这两个时刻自动跑，需要强制重采就重启服务。
          </div>
        </Panel>

        <Panel
          title="iFinD 调用配额"
          meta={
            <span className="num">
              {quota
                ? `本周期 ${fmtShortDate(quota.cycle_start)} ~ ${fmtShortDate(quota.cycle_end)} · 账号级共享`
                : '—'}
            </span>
          }
          delay={80}
        >
          <div className="grid grid-cols-2 overflow-hidden md:grid-cols-4">
            <Cell
              label="本周期已用"
              value={
                quota ? `${fmtInt(quota.cycle_calls)} / ${fmtInt(quota.monthly_quota)}` : '—'
              }
              tone={quotaTone(ratio)}
            />
            <Cell label="剩余" value={quota ? `${fmtInt(quota.cycle_remaining)} 次` : '—'} />
            <Cell
              label="今日已用"
              value={quota ? `${fmtInt(quota.today_calls)} 次` : '—'}
            />
            <Cell
              label="按当前速度预计周期末"
              value={
                quota == null
                  ? '—'
                  : quota.projected_cycle_calls != null
                    ? `${fmtInt(quota.projected_cycle_calls)} 次`
                    : quota.counting_since
                      ? '样本不足'
                      : '暂无记录'
              }
              tone="text-fg-muted"
            />
          </div>

          <div className="border-t border-line-soft px-4 py-3">
            <div className="h-[6px] w-full bg-ink-700">
              <div
                className={`h-full ${quotaBar(ratio)}`}
                style={{ width: `${barPercent}%` }}
              />
            </div>
            <div className="mt-2 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-[13px] text-fg-dim">
              <span className="num">
                已用 {usedPercent}%
                {quota && (
                  <>
                    <span className="mx-1">·</span>
                    本周期 {quota.cycle_trade_days_total} 个交易日
                    {quota.counting_since && quota.counting_since !== quota.cycle_start ? (
                      <>
                        ，计量自 {fmtShortDate(quota.counting_since)} 起（
                        <b className="font-normal text-accent">更早的消耗没有记录</b>）
                      </>
                    ) : (
                      <>，已过 {quota.cycle_trade_days_passed} 个</>
                    )}
                  </>
                )}
              </span>
              <span>
                额度是<b className="font-normal text-fg-muted">账号级共享</b>的，采集与形态选股共用。
                计量窗口按<b className="font-normal text-fg-muted">订阅周期</b>滚动（iFinD
                后台的「计量区间」），不是自然月。超过 80% 会自动停掉形态选股的全市场更新，
                优先保住基础采集。
              </span>
            </div>
          </div>

          {quota && quota.by_tool.length > 0 && (
            <div className="max-h-[220px] overflow-auto border-t border-line-soft">
              <table className="grid-table">
                <thead>
                  <tr>
                    <SortTh sortKey="server" align="left" {...usageSort}>服务</SortTh>
                    <SortTh sortKey="tool" align="left" {...usageSort}>工具</SortTh>
                    <SortTh sortKey="calls" {...usageSort}>本月调用</SortTh>
                  </tr>
                </thead>
                <tbody>
                  {usageRows.map((row) => (
                    <tr key={`${row.server}.${row.tool}`}>
                      <td className="!text-left">
                        <span className="num text-fg-dim">{row.server}</span>
                      </td>
                      <td className="!text-left">
                        <span className="num text-fg-muted">{row.tool}</span>
                      </td>
                      <td>
                        <span className="num text-fg">{fmtInt(row.calls)}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel
          title="数据覆盖"
          meta={<span className="num">各表可回补范围不同，故分别统计</span>}
          delay={120}
        >
          {/* 与其他表格一样套一层 overflow-auto：否则窄视口下这张表会把
              整个页面撑出横向滚动条，而不是自己滚 */}
          <div className="overflow-auto">
            <table className="grid-table">
              <thead>
                <tr>
                  <SortTh sortKey="label" align="left" {...coverageSort}>数据表</SortTh>
                  <SortTh sortKey="days" {...coverageSort}>覆盖交易日</SortTh>
                  <SortTh sortKey="latest" {...coverageSort}>最新日期</SortTh>
                  {/* 说明是整句文字，没有可比的值 */}
                  <th className="!text-left">说明</th>
                </tr>
              </thead>
              <tbody>
                {coverageRows.map((row) => (
                  <tr key={row.label}>
                    <td className="!text-left">
                      <span className="text-fg">{row.label}</span>
                    </td>
                    <td>
                      <span className="num">{fmtInt(row.days)}</span>
                      <span className="ml-1 text-[13px] text-fg-dim">天</span>
                    </td>
                    <td>
                      <span className="num text-fg-muted">{row.latest ?? '—'}</span>
                    </td>
                    <td className="!text-left">
                      <span className="text-[13px] text-fg-dim">{COVERAGE_NOTES[row.label] ?? ''}</span>
                    </td>
                  </tr>
                ))}
                {status === null && (
                  <tr>
                    <td colSpan={4} className="!text-center text-fg-dim">
                      加载中…
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel
          title="手动操作"
          meta={<span className="num">定时任务之外的补充手段</span>}
          delay={160}
        >
          <div className="flex flex-col gap-4 px-4 py-3.5 lg:flex-row lg:items-start">
            {/* 这里原来还有个「立即采集」按钮，**2026-09-28 删掉**（用户要求「删掉手动
                采集按钮，禁止手动采集」）：点一下就白跑一轮采集（约 50 次 iFinD 调用），
                而它本来就不需要手动跑。后端 `POST /api/admin/collect` 也一并删了，
                所以现在没有任何「手动采集」的入口 —— 采集只有每天收盘后那两条定时任务
                （15:05 主采集 + 17:30 补采）。
                真需要强制重采：重启服务（启动补采会补跑）。 */}

            <div className="flex-1 space-y-2">
              <div className="text-[13px] text-fg-muted">历史回补</div>
              <p className="text-[13px] leading-relaxed text-fg-dim">
                指数与龙虎榜可补满半年；<b className="font-normal text-fg-muted">涨停三池受数据源窗口限制，只能覆盖最近 15 个交易日</b>，
                更早的日期对应指标会记为 null（不是 0）。
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[13px] text-fg-dim">起始日期</span>
                <input
                  type="date"
                  value={backfillStart}
                  onChange={(event) => setBackfillStart(event.target.value)}
                  className="num border border-line bg-ink-850 px-2 py-1 text-[13px] text-fg outline-none focus:border-fg-dim"
                />
                {/* 只有起始日期的话想做「只补某一段」就得从那天一路补到最新，
                    补 3 天和补 120 天是两个完全不同的代价 */}
                <span className="text-[13px] text-fg-dim">到</span>
                <input
                  type="date"
                  value={backfillEnd}
                  onChange={(event) => setBackfillEnd(event.target.value)}
                  className="num border border-line bg-ink-850 px-2 py-1 text-[13px] text-fg outline-none focus:border-fg-dim"
                />
                <span className="text-[13px] text-fg-dim">（留空补到最新交易日）</span>
                <button
                  type="button"
                  onClick={() => void runBackfill()}
                  disabled={busy !== null}
                  className="border border-line px-4 py-1.5 text-[13px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {busy === 'backfill' ? '回补中…' : '开始回补'}
                </button>
              </div>
            </div>
          </div>
        </Panel>

        <Panel
          title="采集日志"
          meta={<span className="num">最近 30 条 · 点列头排序</span>}
          delay={200}
        >
          {status === null || status.recent_logs.length === 0 ? (
            <div className="px-4 py-10 text-center text-[14px] text-fg-dim">暂无日志</div>
          ) : (
            <div className="max-h-[420px] overflow-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <SortTh sortKey="created_at" {...logSort}>时间</SortTh>
                    <SortTh sortKey="trade_date" {...logSort}>交易日</SortTh>
                    <SortTh sortKey="task" align="left" {...logSort}>任务</SortTh>
                    <SortTh sortKey="status" align="left" {...logSort}>状态</SortTh>
                    <SortTh sortKey="rows" {...logSort}>行数</SortTh>
                    <SortTh sortKey="cost_seconds" {...logSort}>耗时</SortTh>
                    {/* 消息是整句文字，没有可比的值 */}
                    <th className="!text-left">消息</th>
                  </tr>
                </thead>
                <tbody>
                  {logRows.map((log, index) => (
                    <LogRow key={`${log.created_at}-${log.task}-${index}`} log={log} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        {/* 会员与邀请码。这两块只有管理员看得到这个页面（导航项本身对会员隐藏，
            路由也套了 RequireAdmin）。 */}
        <MembersPanel />
        <InvitePanel />
      </div>
    </Layout>
  )
}

const COVERAGE_NOTES: Record<string, string> = {
  指数日线: '走 iFinD 日频接口，可回补半年',
  涨停三池: '数据源只保留最近 15 个交易日',
  龙虎榜: '接口支持区间查询，可回补半年',
  板块行情: '开盘红口径（精选 + 行业），当日与历史同一条路径',
  涨停题材: '来自开盘红涨停天梯，只覆盖当日涨停股',
  个股日线: '形态选股的底座，只采池内（日均成交额 ≥1 亿）的票',
  情绪指标: '由三池与指数推导，受三池窗口限制',
}

/**
 * 配额的三档着色，阈值与设计文档 8.16.4 的分级让路对齐：
 * 80% 起停形态选股的全市场更新，95% 起只保留指数 + 涨停三池 + 情绪主线。
 * 用 ok/accent/danger 三色而不是 up/down —— 后者在本站是红涨绿跌，语义会打架。
 */
function quotaTone(ratio: number | null): string {
  if (ratio == null) return 'text-fg'
  if (ratio >= 0.95) return 'text-danger'
  if (ratio >= 0.8) return 'text-accent'
  return 'text-ok'
}

function quotaBar(ratio: number | null): string {
  const tone = quotaTone(ratio)
  return tone.replace('text-', 'bg-')
}

function Cell({
  label,
  value,
  tone = 'text-fg',
  pulse = false,
}: {
  label: string
  value: string
  tone?: string
  pulse?: boolean
}) {
  return (
    <div className="relative -mr-px -mb-px border-r border-b border-line-soft px-4 py-3">
      <div className="text-[13px] tracking-[0.1em] text-fg-dim">{label}</div>
      <div className={`num mt-1.5 text-[16px] leading-tight font-medium ${tone}`}>
        {pulse && (
          <span className="pulse-soft mr-1.5 inline-block h-[6px] w-[6px] bg-ok align-middle" />
        )}
        {value}
      </div>
    </div>
  )
}

function LogRow({ log }: { log: CollectLog }) {
  const tone = LOG_TONE[log.status] ?? 'text-fg'
  return (
    <tr>
      <td>
        <span className="num text-fg-dim">{fmtDateTime(log.created_at)}</span>
      </td>
      <td>
        <span className="num text-fg-muted">
          {log.trade_date ? fmtShortDate(log.trade_date) : '—'}
        </span>
      </td>
      <td className="!text-left">
        <span className="num text-fg">{log.task}</span>
      </td>
      <td className="!text-left">
        <span className={`num ${tone}`}>{log.status}</span>
      </td>
      <td>
        <span className="num text-fg-muted">{log.rows ?? '—'}</span>
      </td>
      <td>
        <span className="num text-fg-dim">
          {log.cost_seconds == null ? '—' : `${log.cost_seconds}s`}
        </span>
      </td>
      <td className="!text-left">
        <span
          className="inline-block max-w-[320px] truncate align-bottom text-[13px] text-fg-dim"
          title={log.message ?? ''}
        >
          {log.message ?? '—'}
        </span>
      </td>
    </tr>
  )
}
