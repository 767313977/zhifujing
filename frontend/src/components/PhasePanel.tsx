import type { WudaoPhase } from '../api/types'
import Panel from './Panel'
import { fmtNum, fmtPct } from '../lib/format'

/**
 * 「阶段判定」那一档的颜色。
 *
 * 只分三档、只用站内已有的色：能动手（accent）/ 只观察（up）/ 别碰（down 或弱色）。
 * 别为 8 个阶段各配一个颜色 —— 颜色多了反而看不出哪一档要动手。
 */
const PHASE_TONE: Record<string, string> = {
  start: 'text-accent',
  sample: 'text-up',
  wake: 'text-fg',
  digest: 'text-fg',
  diverge: 'text-down',
  dump: 'text-down',
  silent: 'text-fg-muted',
  unknown: 'text-fg-muted',
}

/**
 * 阶段 key → 色调类名。卡片与**形态选股命中列表的那一列**共用，免得同一个阶段
 * 在两个页面配出两种颜色（`null`／未知 key 一律落到常规前景色）。
 */
export function phaseTone(phase: string | null | undefined): string {
  return (phase ? PHASE_TONE[phase] : undefined) ?? 'text-fg'
}

/**
 * 悟道「阶段判定」卡片（移植自 yangban-desk 的 8 个标签）。
 *
 * 数字全由后端算好（`services/patterns.classify_phase`，与形态选股共用判据），
 * 这里只排版。`metrics` 里几个关键量放在标题栏，免得正文太长还得找。
 *
 * 两个地方用它：**个股页**（跟在资金流向后面）与**个股分析页**（查完票直接看结论）。
 * 抽成组件是因为那两处必须逐字一致 —— 同一只票在两个页面显示不同的「像不像」，
 * 谁也不知道该信哪个。
 */
export default function PhasePanel({
  phase,
  title = '阶段判定',
  delay = 55,
}: {
  phase: WudaoPhase
  /** 面板标题。个股分析页那边会换成「结论」之类，默认沿用个股页的叫法 */
  title?: string
  delay?: number
}) {
  const tone = phaseTone(phase.phase)
  const m = phase.metrics
  return (
    <Panel
      title={title}
      meta={
        <span className="num text-[13px]">
          量比 {fmtNum(m.vol_ratio, 2)}
          <span className="mx-2">·</span>
          冲高 {fmtNum(m.high_pct, 1, '%')}
          <span className="mx-2">·</span>
          上影 {fmtNum(m.upper_shadow, 2)}
          <span className="mx-2">·</span>
          近 5 日 {fmtPct(m.ret_5)}
        </span>
      }
      delay={delay}
    >
      <div className="space-y-2 px-4 py-3.5 text-[13px] leading-relaxed">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className={`text-[15px] font-medium ${tone}`}>{phase.label}</span>
          <span className="text-fg-muted">{phase.action}</span>
          {phase.watch_price != null && (
            <span className="num text-fg">关键价 {fmtNum(phase.watch_price, 2)}</span>
          )}
          <span className="text-fg-dim">像不像：{phase.fit_label}</span>
        </div>
        <p className="text-fg-muted">{phase.text}</p>
        <p className="text-fg-dim">{phase.fit_text}</p>
        <div className="grid gap-x-6 gap-y-1 border-t border-line-soft pt-2 text-[12px] text-fg-dim md:grid-cols-2">
          <p>
            <span className="text-fg-muted">{phase.shadow_label}</span>
            <span className="mx-1">·</span>
            {phase.shadow_text}
          </p>
          <p>
            <span className="text-fg-muted">{phase.pressure_label}</span>
            <span className="mx-1">·</span>
            {phase.pressure_text}
          </p>
          <p>
            <span className="text-fg-muted">计划</span>
            <span className="mx-1">·</span>
            {phase.plan_text}
          </p>
          <p>
            <span className="text-fg-muted">风险</span>
            <span className="mx-1">·</span>
            {phase.risk_text}
          </p>
        </div>
      </div>
    </Panel>
  )
}
