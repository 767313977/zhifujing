import type { RiskBadge } from '../api/types'

/**
 * 风险角标的**渲染**部分（FEAT-RISK-BADGES）。
 *
 * 取数钩子 `useRiskBadges` 在 `lib/riskBadges.ts`：组件与钩子同文件导出会让
 * Fast Refresh 退化成整页重载（2026-11-01 拆开）。
 */

const TONE: Record<string, string> = {
  high: 'border-danger/50 text-danger',
  medium: 'border-warn/50 text-warn',
  low: 'border-line text-fg-muted',
}

export default function RiskMark({
  badges,
  className = '',
}: {
  badges?: RiskBadge[]
  className?: string
}) {
  if (!badges || badges.length === 0) return null
  return (
    <span className={`ml-1 inline-flex flex-wrap gap-1 align-middle ${className}`}>
      {badges.map((badge) => (
        <span
          key={badge.kind}
          title={badge.detail ? `${badge.label}：${badge.detail}` : badge.label}
          className={`border px-1 text-[10px] leading-[16px] ${
            TONE[badge.level] ?? 'border-line text-fg-muted'
          }`}
        >
          {badge.label}
        </span>
      ))}
    </span>
  )
}
