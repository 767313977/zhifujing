import type { ReactNode } from 'react'

/**
 * 结论条外壳（规范 v1 §5，Phase 4/5 共用）。
 *
 * 左边一句结论 + 一行补充，右边一组关键数字；数字全部由调用方从**已有字段**拼出来。
 * 抽成组件是为了让「今日复盘」与「涨停复盘」的结论条长得一样 —— 两页各写一份
 * 迟早会漂（字号、间距、分隔方式都会慢慢分叉）。
 *
 * ⚠️ 这里**不承载任何判断逻辑**：结论文字与数字都由页面按自己的字段模板拼好传进来，
 * 避免出现「组件偷偷算了一个新指标」的情况。
 */
export default function ConclusionBar({
  headline,
  sub,
  stats,
}: {
  /** 一句话结论（纯文本或带色的节点） */
  headline: ReactNode
  /** 第二行：补充说明 / 口径 */
  sub?: ReactNode
  /** 右侧关键数字，通常是一组 <ConclusionStat> */
  stats: ReactNode
}) {
  return (
    <section className="panel px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="text-[12px] tracking-[0.1em] text-fg-dim">结论</span>
            <span className="text-[15px] font-semibold text-fg">{headline}</span>
          </div>
          {sub != null && (
            <div className="num mt-1 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-[12px] text-fg-dim">
              {sub}
            </div>
          )}
        </div>
        <div className="num ml-auto flex flex-wrap items-baseline gap-x-5 gap-y-1">
          {stats}
        </div>
      </div>
    </section>
  )
}

/** 结论条里的一个「标签 + 数值」。 */
export function ConclusionStat({
  label,
  value,
  tone,
  title,
}: {
  label: string
  value: string
  tone?: string
  title?: string
}) {
  return (
    <span className="inline-flex items-baseline gap-1.5" title={title}>
      <span className="text-[12px] text-fg-dim">{label}</span>
      <span className={`num text-[13px] ${tone ?? 'text-fg'}`}>{value}</span>
    </span>
  )
}
