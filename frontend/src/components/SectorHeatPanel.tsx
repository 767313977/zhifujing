import type { SectorHeat, SectorHeatItem } from '../api/types'
import { fmtAmount, fmtPct, toneOf } from '../lib/format'
import Panel from './Panel'

/** 涨跌幅绝对值到这个数就算「最热」，色块背景打满。 */
const FULL_HEAT = 4

interface SectorHeatPanelProps {
  heat: SectorHeat
  delay?: number
}

/**
 * 首页板块热力。
 *
 * 用「色块深浅」而不是条形长度表达强度：首页只回答「今天什么在涨、什么在跌」，
 * 色块可以在一行里塞下更多板块，扫一眼就能看出方向。
 * 背景不用红/绿的实色，而是按涨跌幅叠加透明度 —— 避免整块高饱和让人分不清强弱。
 */
export default function SectorHeatPanel({ heat, delay = 0 }: SectorHeatPanelProps) {
  const { selected, industry } = heat

  return (
    <Panel
      title="板块热力"
      meta={
        <span className="num">
          精选 {selected.rising} 涨 / {selected.falling} 跌 · 均值{' '}
          {fmtPct(selected.average)}
        </span>
      }
      delay={delay}
    >
      <div className="space-y-2 px-3 py-3">
        <Row label="领涨" items={selected.leaders} />
        <Row label="领跌" items={selected.laggards} />
      </div>

      <div className="border-t border-line-soft px-3 py-2 text-[13px] text-fg-dim">
        <span className="mr-2">行业</span>
        <span className="num text-fg-muted">
          {industry.rising} 涨 / {industry.falling} 跌 · 均值 {fmtPct(industry.average)}
        </span>
        {industry.leaders[0] && (
          <span className="ml-3">
            最强
            <span className="num ml-1 text-fg">{industry.leaders[0].name}</span>
            <span className={`num ml-1 ${toneOf(industry.leaders[0].pct_chg)}`}>
              {fmtPct(industry.leaders[0].pct_chg)}
            </span>
          </span>
        )}
        {industry.laggards[0] && (
          <span className="ml-3">
            最弱
            <span className="num ml-1 text-fg">{industry.laggards[0].name}</span>
            <span className={`num ml-1 ${toneOf(industry.laggards[0].pct_chg)}`}>
              {fmtPct(industry.laggards[0].pct_chg)}
            </span>
          </span>
        )}
      </div>
    </Panel>
  )
}

function Row({ label, items }: { label: string; items: SectorHeatItem[] }) {
  return (
    <div className="flex items-start gap-2">
      <span className="mt-1.5 w-7 shrink-0 text-[13px] text-fg-dim">{label}</span>
      <div className="grid flex-1 grid-cols-2 gap-1 sm:grid-cols-3 lg:grid-cols-5">
        {items.map((item) => (
          <div
            key={item.code}
            className="border border-line-soft px-2 py-1.5"
            style={{ backgroundColor: heatColor(item.pct_chg) }}
            title={`${item.name} ${fmtPct(item.pct_chg)} · 成交额 ${fmtAmount(item.amount)}${
              item.limit_up_count ? ` · 涨停 ${item.limit_up_count} 只` : ''
            }`}
          >
            <div className="flex items-baseline justify-between gap-1.5">
              <span className="truncate text-[13px] text-fg">{item.name}</span>
              <span className={`num shrink-0 text-[13px] ${toneOf(item.pct_chg)}`}>
                {fmtPct(item.pct_chg)}
              </span>
            </div>
            {item.limit_up_count ? (
              <div className="num text-[12px] text-fg-dim">
                涨停 {item.limit_up_count}
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  )
}

/**
 * A 股惯例红涨绿跌，底色按幅度叠加透明度 —— 只用**极低透明度的蒙层**，
 * 不用实色块：整块高饱和会让人分不清强弱，也把这一屏的颜色预算烧光。
 *
 * ⚠️ 这里的 RGB 是 `--color-up` / `--color-down` 的**副本**（内联样式里没法写
 * CSS 变量参与的计算）。改那两个令牌时必须同步这两个数，否则板块热力的底色
 * 会留在旧的红绿上，和页面其它地方对不上。
 */
function heatColor(value: number | null): string | undefined {
  if (value == null || value === 0) return undefined
  const intensity = Math.min(Math.abs(value) / FULL_HEAT, 1)
  const rgb = value > 0 ? '240, 90, 77' : '34, 181, 115'
  return `rgba(${rgb}, ${(0.05 + intensity * 0.18).toFixed(3)})`
}
