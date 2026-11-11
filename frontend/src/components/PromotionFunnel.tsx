import { useEffect, useMemo, useState } from 'react'
import type { PromotionFunnel as Funnel } from '../api/types'
import { fmtInt, fmtNum } from '../lib/format'
import PromotionFunnelQuality from './PromotionFunnelQuality'
import PromotionFunnelStockTable from './PromotionFunnelStockTable'

/** 百分比：null 一律「—」，不能写 0%。 */
function rateText(value: number | null): string {
  return value == null ? '—' : `${fmtNum(value, 1)}%`
}

export default function PromotionFunnel({
  data,
  loading,
  error,
}: {
  data: Funnel | null
  loading: boolean
  error: string | null
}) {
  const [selectedLevel, setSelectedLevel] = useState<number | null>(null)

  useEffect(() => {
    if (!data) {
      setSelectedLevel(null)
      return
    }
    const first = data.levels.find((item) => item.base > 0) ?? data.levels[0]
    setSelectedLevel(first?.level ?? null)
  }, [data])

  const totals = useMemo(() => {
    const levels = data?.levels ?? []
    return levels.reduce(
      (acc, item) => ({
        base: acc.base + item.base,
        promoted: acc.promoted + item.promoted,
        broken: acc.broken + item.broken,
        failed: acc.failed + item.failed,
        unverifiable: acc.unverifiable + item.unverifiable,
        validBase: acc.validBase + item.valid_base,
      }),
      { base: 0, promoted: 0, broken: 0, failed: 0, unverifiable: 0, validBase: 0 },
    )
  }, [data])

  if (loading && !data) {
    return <div className="px-4 py-10 text-center text-[14px] text-fg-dim">加载中…</div>
  }
  if (error) {
    return <div className="px-4 py-10 text-center text-[14px] text-danger">{error}</div>
  }
  if (!data) {
    return (
      <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
        暂无晋级漏斗数据
      </div>
    )
  }

  const current = data.levels.find((item) => item.level === selectedLevel) ?? null
  const overallLegacy = totals.base > 0 ? (totals.promoted / totals.base) * 100 : null
  const overallVerified =
    totals.validBase > 0 ? (totals.promoted / totals.validBase) * 100 : null

  return (
    <div>
      <PromotionFunnelQuality
        quality={data.quality}
        tradeDate={data.trade_date}
        prevDate={data.prev_date}
      />

      <div className="flex flex-wrap items-stretch gap-px border-b border-line-soft bg-line-soft">
        <Stat label="昨日基数" value={fmtInt(totals.base)} unit="只" />
        <Stat label="今日晋级" value={fmtInt(totals.promoted)} unit="只" tone="text-up" />
        <Stat label="今日炸板" value={fmtInt(totals.broken)} unit="只" tone="text-warn" />
        <Stat label="今日失败" value={fmtInt(totals.failed)} unit="只" />
        <Stat
          label="不可验证"
          value={fmtInt(totals.unverifiable)}
          unit="只"
          tone={totals.unverifiable > 0 ? 'text-danger' : 'text-fg'}
        />
        <Stat label="原口径晋级率" value={rateText(overallLegacy)} />
        <Stat label="可验证口径" value={rateText(overallVerified)} />
      </div>

      {data.levels.length === 0 ? (
        <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
          {data.quality.status === 'NOT_COLLECTED'
            ? '该交易日没有可用的昨日涨停池，无法建立晋级基数。'
            : '该交易日缺少可信的昨日涨停池，未生成漏斗。'}
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-2 border-b border-line-soft p-3 sm:grid-cols-2 xl:grid-cols-4">
            {data.levels.map((item) => (
              <button
                key={item.level}
                type="button"
                aria-pressed={item.level === selectedLevel}
                aria-label={`档位 ${item.label}`}
                onClick={() => setSelectedLevel(item.level)}
                className={[
                  'border px-3 py-2 text-left transition-colors',
                  item.level === selectedLevel
                    ? 'border-accent/60 bg-accent/5'
                    : 'border-line hover:border-fg-dim',
                ].join(' ')}
              >
                <div className="flex items-baseline justify-between">
                  <span className="text-[14px] text-fg">{item.label}</span>
                  <span className="num text-[12px] text-fg-dim">基数 {item.base}</span>
                </div>
                <div className="mt-1.5 flex flex-wrap gap-x-2 gap-y-0.5 text-[12px]">
                  <span className="text-up">晋级 {item.promoted}</span>
                  <span className="text-warn">炸板 {item.broken}</span>
                  <span className="text-fg-muted">失败 {item.failed}</span>
                  <span className={item.unverifiable > 0 ? 'text-danger' : 'text-fg-dim'}>
                    不可验证 {item.unverifiable}
                  </span>
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3 text-[12px] text-fg-dim">
                  <span>原口径 {rateText(item.legacy_rate)}</span>
                  <span>可验证 {rateText(item.verified_rate)}</span>
                </div>
              </button>
            ))}
          </div>

          {current && (
            <div>
              <div className="flex flex-wrap items-baseline gap-2 px-3 py-2 text-[12px] text-fg-dim">
                <span className="text-[13px] text-fg">{current.label}</span>
                <span className="num">
                  基数 {current.base} · 有效样本 {current.valid_base} · 晋级{' '}
                  {current.promoted} · 炸板 {current.broken} · 失败 {current.failed} ·
                  不可验证 {current.unverifiable}
                </span>
                <span className="ml-auto">
                  原口径 = 晋级 / 昨日基数；可验证口径 = 晋级 / 有效样本
                </span>
              </div>
              <PromotionFunnelStockTable stocks={current.stocks} />
            </div>
          )}
        </>
      )}

      <p className="border-t border-line-soft px-4 py-2 text-[12px] leading-relaxed text-fg-dim">
        只有今日涨停池、炸板池与个股日线都可核对时，才把「不在两个池」判为失败；
        昨日池不可信时只展示观察基数，不生成成功率。晋级按今日连板数 = 昨日板高 + 1
        严格判定，不用最高价是否触板代替。
      </p>
    </div>
  )
}

function Stat({
  label,
  value,
  unit,
  tone = 'text-fg',
}: {
  label: string
  value: string
  unit?: string
  tone?: string
}) {
  return (
    <div className="min-w-[120px] flex-1 bg-ink-900 px-4 py-2.5">
      <div className="text-[12px] tracking-[0.08em] text-fg-dim">{label}</div>
      <div className={`num mt-1 text-[20px] font-semibold leading-tight ${tone}`}>
        {value}
        {unit && <span className="ml-1 text-[12px] font-normal text-fg-dim">{unit}</span>}
      </div>
    </div>
  )
}
