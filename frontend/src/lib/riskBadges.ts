/**
 * 批量风险角标的取数钩子（2026-11-01 从 `components/RiskBadges.tsx` 拆出）。
 *
 * 为什么单独一个文件：渲染角标的 `RiskMark` 是组件，钩子和它同文件导出会让
 * Fast Refresh 退化成整页重载（列表页一次要重拉几十只角标）。拆开后组件文件
 * 只导出组件。
 *
 * 一次请求最多 50 个代码，**后端只读缓存、不打外部接口**；没有缓存就不出角标。
 * 列表页一屏几十只走一个请求，不要逐行调用（那会变成 N 次请求）。
 */

import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { RiskBadge } from '../api/types'

/** 没有代码 / 还没取回时的空表。**必须是同一个引用**：每次 `new Map()` 会让
 *  依赖它的 memo 白算一遍。 */
const EMPTY: Map<string, RiskBadge[]> = new Map()

export function useRiskBadges(codes: string[]): Map<string, RiskBadge[]> {
  const [map, setMap] = useState<Map<string, RiskBadge[]>>(EMPTY)
  const key = useMemo(
    () => [...new Set(codes.filter(Boolean))].slice(0, 50).sort().join(','),
    [codes],
  )
  useEffect(() => {
    if (!key) return
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
  // 没有代码时直接给空表（渲染期派生，不在 effect 里 setState 清空）
  return key ? map : EMPTY
}
