import type { SectorHeat, SectorHeatItem } from '../api/types'
import { withAlpha } from '../lib/chart'
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
 * 背景不用红/青的实色，而是把**同色相的深色底**按涨跌幅叠加透明度 —— 深色底不抬
 * 亮度，卡里的字才守得住对比度（亮色蒙层为什么不行，算据见下面 heatColor 的注释）。
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
              {/* 数字不染涨跌色：底色已经在表达方向，再染一次是重复信息，
                  而且红字压红底必然掉到 AA 线下（算据见 heatColor 的注释）。
                  层级改由字重承担 —— 名称常规、数字中粗。 */}
              <span className="num shrink-0 text-[13px] font-medium text-fg">
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
 * 卡片底色：方向用色相（红涨 / 青跌），强度用透明度。
 *
 * 透明蒙层一律经 `withAlpha` 生成、不手写 RGB 字面量 ——
 * 之前这里写死过一份 `'201, 96, 85'`，改色时就得记着同步它，漏了就只剩这一处旧色。
 *
 * ## 2026-09-26 改法：亮色蒙层 → 同色相**深色**蒙层
 *
 * 卡里有两行字：主数字、「涨停 N」（后者是 `fg-dim`，全站最弱的一档）。亮色蒙层会
 * 抬背景亮度，把最弱那行直接压到 AA 线下 —— 实测：
 *
 * | 蒙层 | α=0.16 | α=0.23 |
 * | --- | --- | --- |
 * | 涨色 `#ff7566` | fg 9.7 / fg-dim **4.2** | fg 8.5 / fg-dim **3.7** |
 * | 跌色 `#45bfc4` | fg 9.1 / fg-dim **4.0** | fg 7.8 / fg-dim **3.4** |
 *
 * 所以蒙层改用同色相的**深色底**（下面两个常量）：深色底不抬亮度，两行字在
 * **整个强度区间**都过线（fg ≥10.6、fg-dim ≥4.6）。
 *
 * ⚠️ 这张表是**按当前涨跌色实测的**。它是「蒙层为什么不能用亮色」的唯一依据，
 * 动了 `--color-up` / `--color-down` 就得重算 —— 别让注释里的数变成假的。
 * （更早一版涨色是 `#fe3330` 时，两行都更低，连主数字都保不住。）
 *
 * 两个基底的亮度是**对齐过的**（0.0216 vs 0.0241，差 1.12 倍）：否则同一强度下
 * 跌卡会比涨卡亮，「领跌」那排看着反而更热。
 *
 * 主数字用中性 `text-fg`、不再染色（用户 2026-09-26 拍板）：底色已经在表达方向与
 * 强弱，再染一次是重复信息，而且染成涨跌色就会往上面那张表的坑里走。同花顺自己的
 * 板块热力也是「彩块 + 白字」。下方「涨停 N」那行仍是 fg-dim，它有 4.6:1，够用。
 */
const HEAT_UP = '#4d1512'
const HEAT_DOWN = '#0a3032'

function heatColor(value: number | null): string | undefined {
  if (value == null || value === 0) return undefined
  const intensity = Math.min(Math.abs(value) / FULL_HEAT, 1)
  return withAlpha(value > 0 ? HEAT_UP : HEAT_DOWN, 0.2 + intensity * 0.6)
}
