import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { RiskPref, RiskStatus } from '../api/types'
import Alert from './Alert'
import Panel from './Panel'

/**
 * 风控提醒（FEAT-RISK-GUARD，2026-10-31）。
 *
 * **只提示，不阻止操作**；偏好按用户存库（`/api/risk/pref`），状态现算
 * （`/api/risk/status`：连板高度 / 恐贪指数 / 持仓集中度）。
 */

const INPUT =
  'num w-full border border-line bg-ink-850 px-2 py-[5px] text-[13px] text-fg outline-none focus:border-fg-dim'

/** 列表页顶部的提示条：触发过热或集中度超限才出现。 */
export function RiskBanner() {
  const [status, setStatus] = useState<RiskStatus | null>(null)
  useEffect(() => {
    api
      .riskStatus()
      .then(setStatus)
      .catch(() => setStatus(null))
  }, [])
  if (!status || (!status.overheat && status.concentration.length === 0)) return null
  return (
    <div
      className={`border px-4 py-2 text-[13px] ${
        status.overheat
          ? 'border-danger/50 bg-danger/5 text-danger'
          : 'border-warn/50 bg-warn/5 text-warn'
      }`}
    >
      <span className="mr-2">{status.strict_mode ? '严格模式' : '风控提醒'}</span>
      {status.message}
    </div>
  )
}

/** 账号页的风控偏好表单。 */
export function RiskPrefPanel() {
  const [pref, setPref] = useState<RiskPref | null>(null)
  const [status, setStatus] = useState<RiskStatus | null>(null)
  const [form, setForm] = useState({
    position: '',
    theme: '',
    consecutive: '',
    temperature: '',
    strict: false,
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const fill = (value: RiskPref) => {
    setPref(value)
    setForm({
      position: value.max_position_pct == null ? '' : String(value.max_position_pct),
      theme: value.max_theme_pct == null ? '' : String(value.max_theme_pct),
      consecutive: String(value.heat_consecutive),
      temperature: String(value.heat_temperature),
      strict: value.strict_mode,
    })
  }

  useEffect(() => {
    api
      .riskPref()
      .then(fill)
      .catch((err: Error) => setError(err.message))
    api
      .riskStatus()
      .then(setStatus)
      .catch(() => setStatus(null))
  }, [])

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      const saved = await api.saveRiskPref({
        max_position_pct: form.position.trim() ? Number(form.position) : null,
        max_theme_pct: form.theme.trim() ? Number(form.theme) : null,
        heat_consecutive: Number(form.consecutive) || 6,
        heat_temperature: Number(form.temperature) || 80,
        strict_mode: form.strict,
      })
      fill(saved)
      setNotice('风控偏好已保存')
      api
        .riskStatus()
        .then(setStatus)
        .catch(() => setStatus(null))
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}
      {notice && (
        <Alert tone="accent" onClose={() => setNotice(null)}>
          {notice}
        </Alert>
      )}
      <Panel
        title="风控偏好"
        meta={<span className="num">仅提示，不阻止操作</span>}
        delay={70}
      >
        {pref === null ? (
          <div className="px-4 py-6 text-center text-[13px] text-fg-dim">加载中…</div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3 px-4 py-3 md:grid-cols-4">
              <label className="flex flex-col gap-1">
                <span className="text-[12px] text-fg-dim">单票仓位上限（%）</span>
                <input
                  type="number"
                  value={form.position}
                  onChange={(event) =>
                    setForm((prev) => ({ ...prev, position: event.target.value }))
                  }
                  placeholder="留空不提醒"
                  className={INPUT}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[12px] text-fg-dim">单题材上限（%）</span>
                <input
                  type="number"
                  value={form.theme}
                  onChange={(event) =>
                    setForm((prev) => ({ ...prev, theme: event.target.value }))
                  }
                  placeholder="留空不提醒"
                  className={INPUT}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[12px] text-fg-dim">连板高度告警（板）</span>
                <input
                  type="number"
                  value={form.consecutive}
                  onChange={(event) =>
                    setForm((prev) => ({ ...prev, consecutive: event.target.value }))
                  }
                  className={INPUT}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[12px] text-fg-dim">恐贪指数告警（0-100）</span>
                <input
                  type="number"
                  value={form.temperature}
                  onChange={(event) =>
                    setForm((prev) => ({ ...prev, temperature: event.target.value }))
                  }
                  className={INPUT}
                />
              </label>
            </div>
            <div className="flex flex-wrap items-center gap-3 px-4 pb-3">
              <label className="flex items-center gap-2 text-[13px] text-fg-muted">
                <input
                  type="checkbox"
                  checked={form.strict}
                  onChange={(event) =>
                    setForm((prev) => ({ ...prev, strict: event.target.checked }))
                  }
                />
                严格模式（默认只提示，不会阻止下单操作）
              </label>
              <button
                type="button"
                onClick={() => void save()}
                disabled={saving}
                className="border border-accent/60 bg-accent/10 px-3 py-[6px] text-[13px] text-accent hover:bg-accent/20 disabled:opacity-40"
              >
                {saving ? '保存中…' : '保存偏好'}
              </button>
            </div>
            <div className="border-t border-line-soft px-4 py-2 text-[12px] text-fg-dim">
              当前状态：{status ? status.message : '—'}
            </div>
          </>
        )}
      </Panel>
    </>
  )
}
