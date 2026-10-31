import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { RiskBadge } from '../api/types'

/**
 * 批量风险角标（FEAT-RISK-BADGES）。
 *
 * 一次请求最多 50 个代码，**后端只读缓存、不打外部接口**；没有缓存就不出角标。
 * 列表页一屏几十只走一个请求，不要逐行调用（那会变成 N 次请求）。
 */
export function useRiskBadges(codes: string[]): Map<string, RiskBadge[]> {
  const [map, setMap] = useState<Map<string, RiskBadge[]>>(new Map())
  const key = useMemo(
    () => [...new Set(codes.filter(Boolean))].slice(0, 50).sort().join(','),
    [codes],
  )
  useEffect(() => {
    if (!key) {
      setMap(new Map())
      return
    }
    let stale = false
    api
      .riskBadges(key.split(','))
      .then((data) => {
        if (stale) return
        setMap(new Map(data.rows.map((row) => [row.code, row.badges])))
      })
      .catch(() => {
        // 角标是增强信息：失败静默，不影响列表
      })
    return () => {
      stale = true
    }
  }, [key])
  return map
}

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
