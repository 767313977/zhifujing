/**
 * 「快速研究」按钮（S1-C4 / S3-C）。
 *
 * 放在个股代码 / 名称旁边，点开全局研究侧栏。它**不改变原有的个股链接行为**：
 * 点击时会 `preventDefault + stopPropagation`，因此不会触发行级 `rememberStockList`
 * 或 `<Link>` 导航，也不会产生双重路由跳转。
 *
 * S3 起可以指定打开哪个 Tab（例如列表里的「据」直接打开证据页），
 * 默认仍是「研 → 概览」，旧调用不需要改。
 */

import { useStockResearch } from '../lib/stockResearchContext'
import type { ResearchTab } from '../lib/stockResearchContext'

export default function QuickResearch({
  code,
  asOfDate,
  tab,
  label = '研',
  className = '',
}: {
  code: string
  /** 页面当前的数据日（可选）：侧栏缓存键会带上它，翻日期不会复用旧数据 */
  asOfDate?: string | null
  /** 打开哪个 Tab；默认概览 */
  tab?: ResearchTab
  /** 按钮文字；默认「研」 */
  label?: string
  className?: string
}) {
  const { openStockResearch } = useStockResearch()
  return (
    <button
      type="button"
      title={label === '研' ? '快速研究（侧栏，只读）' : '打开当日研究依据（只读）'}
      aria-label={`快速研究 ${code}${label === '研' ? '' : `（${label}）`}`}
      onClick={(event) => {
        event.preventDefault()
        event.stopPropagation()
        openStockResearch(code, { asOfDate: asOfDate ?? null, tab })
      }}
      className={[
        'ml-1 shrink-0 border border-line px-1 text-[11px] leading-[16px] text-fg-dim',
        'transition-colors hover:border-accent/60 hover:text-accent',
        className,
      ].join(' ')}
    >
      {label}
    </button>
  )
}
