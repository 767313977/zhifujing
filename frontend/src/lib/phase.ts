/**
 * 悟道「阶段判定」的配色（2026-11-01 从 `components/PhasePanel.tsx` 拆出）。
 *
 * 为什么单独一个文件：`PhasePanel.tsx` 导出组件，同时导出 `phaseTone` 会让
 * Fast Refresh 退化成整页重载（`react/only-export-components`）。
 * 卡片与**形态选股命中列表的那一列**共用同一份判据，避免同一阶段配出两种颜色。
 */

/**
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
  weak: 'text-down',
  silent: 'text-fg-muted',
  unknown: 'text-fg-muted',
}

/**
 * 阶段 key → 色调类名。
 *
 * 卡片与形态选股命中列表共用一个来源（`null`／未知 key 一律落到常规前景色）。
 */
export function phaseTone(phase: string | null | undefined): string {
  return (phase ? PHASE_TONE[phase] : undefined) ?? 'text-fg'
}
