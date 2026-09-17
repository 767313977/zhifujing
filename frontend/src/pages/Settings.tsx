import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { AdminStatus, CollectLog } from '../api/types'
import Alert from '../components/Alert'
import Layout from '../components/Layout'
import Panel from '../components/Panel'
import { fmtInt, fmtShortDate } from '../lib/format'

/** ISO 时间戳 → 09-18 15:05 */
function fmtDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  const matched = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/)
  if (!matched) return value
  const [, , month, day, hour, minute] = matched
  return `${month}-${day} ${hour}:${minute}`
}

function sixMonthsAgo(): string {
  const now = new Date()
  now.setMonth(now.getMonth() - 6)
  return now.toISOString().slice(0, 10)
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
  const [busy, setBusy] = useState<'collect' | 'backfill' | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(() => {
    api
      .adminStatus()
      .then(setStatus)
      .catch((err: Error) => setError(err.message))
  }, [])

  useEffect(() => {
    reload()
  }, [reload])

  const runCollect = useCallback(async () => {
    setBusy('collect')
    setError(null)
    setMessage(null)
    try {
      const result = await api.collect()
      const failed = Object.entries(result.steps).filter(([, s]) => s.status !== 'ok')
      setMessage(
        failed.length === 0
          ? `采集完成：${result.trade_date}，${Object.keys(result.steps).length} 个步骤全部成功`
          : `采集部分失败：${failed.map(([n, s]) => `${n}(${s.status})`).join('；')}`,
      )
      reload()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(null)
    }
  }, [reload])

  const runBackfill = useCallback(async () => {
    setBusy('backfill')
    setError(null)
    setMessage('回补中，逐日补数可能持续几分钟，请勿关闭页面…')
    try {
      const result = await api.backfill(backfillStart)
      setMessage(
        `回补完成：处理 ${result.days} 个交易日，失败步骤 ${result.failed_steps} 个`,
      )
      reload()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(null)
    }
  }, [backfillStart, reload])

  const scheduler = status?.scheduler

  const toolbar = (
    <button
      type="button"
      onClick={reload}
      className="num border border-line px-2.5 py-[3px] text-[12px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg"
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
          <div className="border-t border-line-soft px-4 py-2.5 text-[12px] leading-relaxed text-fg-dim">
            采集时刻设为 15:05 是为了等收盘数据稳定。
            {scheduler?.catchup_on_start && (
              <>
                <span className="mx-1">·</span>
                已开启<b className="font-normal text-fg-muted">启动补采</b>：本机不常开，
                错过采集时刻后下次启动会在后台补上。
              </>
            )}
            <span className="mx-1">·</span>
            定时与手动采集<b className="font-normal text-fg-muted">互斥</b>，
            不会重复请求数据源。
          </div>
        </Panel>

        <Panel
          title="数据覆盖"
          meta={<span className="num">各表可回补范围不同，故分别统计</span>}
          delay={80}
        >
          <table className="grid-table">
            <thead>
              <tr>
                <th className="!text-left">数据表</th>
                <th>覆盖交易日</th>
                <th>最新日期</th>
                <th className="!text-left">说明</th>
              </tr>
            </thead>
            <tbody>
              {(status?.coverage ?? []).map((row) => (
                <tr key={row.label}>
                  <td className="!text-left">
                    <span className="text-fg">{row.label}</span>
                  </td>
                  <td>
                    <span className="num">{fmtInt(row.days)}</span>
                    <span className="ml-1 text-[11px] text-fg-dim">天</span>
                  </td>
                  <td>
                    <span className="num text-fg-muted">{row.latest ?? '—'}</span>
                  </td>
                  <td className="!text-left">
                    <span className="text-[11px] text-fg-dim">{COVERAGE_NOTES[row.label] ?? ''}</span>
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
        </Panel>

        <Panel
          title="手动操作"
          meta={<span className="num">定时任务之外的补充手段</span>}
          delay={120}
        >
          <div className="flex flex-col gap-4 px-4 py-3.5 lg:flex-row lg:items-start">
            <div className="flex-1 space-y-2">
              <div className="text-[12px] text-fg-muted">采集最近交易日</div>
              <p className="text-[12px] leading-relaxed text-fg-dim">
                拉取指数行情、涨停三池、龙虎榜与情绪指标。已采集过的日期会被幂等覆盖，重复点不会产生脏数据。
              </p>
              <button
                type="button"
                onClick={() => void runCollect()}
                disabled={busy !== null}
                className="border border-accent/60 bg-accent/10 px-4 py-1.5 text-[12px] text-accent transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy === 'collect' ? '采集中…' : '立即采集'}
              </button>
            </div>

            <div className="hidden w-px self-stretch bg-line-soft lg:block" />

            <div className="flex-1 space-y-2">
              <div className="text-[12px] text-fg-muted">历史回补</div>
              <p className="text-[12px] leading-relaxed text-fg-dim">
                指数与龙虎榜可补满半年；<b className="font-normal text-fg-muted">涨停三池受数据源窗口限制，只能覆盖最近 15 个交易日</b>，
                更早的日期对应指标会记为 null（不是 0）。
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[12px] text-fg-dim">起始日期</span>
                <input
                  type="date"
                  value={backfillStart}
                  onChange={(event) => setBackfillStart(event.target.value)}
                  className="num border border-line bg-ink-850 px-2 py-1 text-[12px] text-fg outline-none focus:border-fg-dim"
                />
                <button
                  type="button"
                  onClick={() => void runBackfill()}
                  disabled={busy !== null}
                  className="border border-line px-4 py-1.5 text-[12px] text-fg-muted transition-colors hover:border-fg-dim hover:text-fg disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {busy === 'backfill' ? '回补中…' : '开始回补'}
                </button>
              </div>
            </div>
          </div>
        </Panel>

        <Panel
          title="采集日志"
          meta={<span className="num">最近 30 条 · 按时间倒序</span>}
          delay={160}
        >
          {status === null || status.recent_logs.length === 0 ? (
            <div className="px-4 py-10 text-center text-[13px] text-fg-dim">暂无日志</div>
          ) : (
            <div className="max-h-[420px] overflow-auto">
              <table className="grid-table">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>交易日</th>
                    <th className="!text-left">任务</th>
                    <th className="!text-left">状态</th>
                    <th>行数</th>
                    <th>耗时</th>
                    <th className="!text-left">消息</th>
                  </tr>
                </thead>
                <tbody>
                  {status.recent_logs.map((log, index) => (
                    <LogRow key={`${log.created_at}-${log.task}-${index}`} log={log} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
    </Layout>
  )
}

const COVERAGE_NOTES: Record<string, string> = {
  指数日线: '走 iFinD 日频接口，可回补半年',
  涨停三池: '数据源只保留最近 15 个交易日',
  龙虎榜: '接口支持区间查询，可回补半年',
  情绪指标: '由三池与指数推导，受三池窗口限制',
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
      <div className="text-[11px] tracking-[0.1em] text-fg-dim">{label}</div>
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
          className="inline-block max-w-[320px] truncate align-bottom text-[11px] text-fg-dim"
          title={log.message ?? ''}
        >
          {log.message ?? '—'}
        </span>
      </td>
    </tr>
  )
}
