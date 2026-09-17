import type { ReactNode } from 'react'

interface AlertProps {
  /** danger 用于错误，accent 用于提示。 */
  tone?: 'danger' | 'accent'
  children: ReactNode
  onClose?: () => void
}

/**
 * 页面顶部提示条。
 *
 * 注意 danger 走的是语义色 `danger` 而不是 `down` —— 本站在 A 股惯例下
 * `down` 是绿色（代表下跌），拿它表示错误会读成相反的意思。
 */
export default function Alert({ tone = 'danger', children, onClose }: AlertProps) {
  const styles =
    tone === 'danger'
      ? 'border-danger/40 bg-danger/5 text-danger'
      : 'border-accent/30 bg-accent/[0.04] text-fg-muted'

  return (
    <div
      className={`rise mb-4 flex items-start gap-2.5 border px-4 py-3 text-[13px] ${styles}`}
    >
      <span className="min-w-0 flex-1 leading-relaxed">{children}</span>
      {onClose && (
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭"
          className="shrink-0 text-fg-dim transition-colors hover:text-fg"
        >
          ✕
        </button>
      )}
    </div>
  )
}
