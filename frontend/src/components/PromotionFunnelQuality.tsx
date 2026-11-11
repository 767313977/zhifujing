import type { PromotionFunnelQuality as Quality } from '../api/types'

const STATUS_LABEL: Record<string, string> = {
  VERIFIED: '已验证',
  PARTIAL: '部分可验证',
  UNVERIFIED: '不可验证',
  NOT_COLLECTED: '未采集',
}

const STATUS_CLASS: Record<string, string> = {
  VERIFIED: 'border-up/40 text-up',
  PARTIAL: 'border-warn/50 text-warn',
  UNVERIFIED: 'border-danger/50 text-danger',
  NOT_COLLECTED: 'border-line text-fg-dim',
}

const REASON_LABEL: Record<string, string> = {
  PREVIOUS_UP_POOL_MISSING: '昨日涨停池无数据',
  PREVIOUS_UP_POOL_UNVERIFIED: '昨日涨停池采集状态不可信',
  TODAY_UP_POOL_MISSING: '今日涨停池无数据',
  TODAY_UP_POOL_UNVERIFIED: '今日涨停池采集状态不可信',
  TODAY_BROKEN_POOL_MISSING: '今日炸板池无数据',
  TODAY_BROKEN_POOL_UNVERIFIED: '今日炸板池采集状态不可信',
  DAILY_QUOTE_MISSING: '部分股票缺少今日日线',
  POOL_CONFLICT: '部分股票同时出现在涨停与炸板池',
  CONSECUTIVE_MISMATCH: '部分股票连板高度不衔接',
  COLLECTION_FAILED: '采集失败',
  COLLECTION_SKIPPED: '采集被跳过',
  SOURCE_STATUS_UNKNOWN: '缺少采集状态证据',
  NO_PREVIOUS_TRADE_DAY: '没有上一交易日',
}

export default function PromotionFunnelQuality({
  quality,
  tradeDate,
  prevDate,
}: {
  quality: Quality
  tradeDate: string
  prevDate: string | null
}) {
  const reasons = [...new Set(quality.reasons)]
  return (
    <div className="border-b border-line-soft px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2 text-[13px]">
        <span
          className={`border px-2 py-[2px] ${STATUS_CLASS[quality.status] ?? 'border-line text-fg-dim'}`}
        >
          {STATUS_LABEL[quality.status] ?? quality.status}
        </span>
        <span className="num text-fg-muted">
          观察日 {tradeDate}
          {prevDate ? ` · 基准日 ${prevDate}` : ''}
        </span>
        <span className="text-fg-dim">
          只有「今日涨停池 + 炸板池 + 个股日线」都可核对时，才把未涨停判为失败。
        </span>
      </div>
      {reasons.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[12px] text-warn">
          {reasons.map((reason) => (
            <span key={reason}>{REASON_LABEL[reason] ?? reason}</span>
          ))}
        </div>
      )}
    </div>
  )
}
