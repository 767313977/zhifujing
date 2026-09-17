import type { ReactNode } from 'react'

interface PanelProps {
  title: string
  /** 标题右侧的补充信息（数量、口径说明等） */
  meta?: ReactNode
  children: ReactNode
  className?: string
  /** 首屏错峰浮现的延迟（毫秒） */
  delay?: number
}

export default function Panel({
  title,
  meta,
  children,
  className = '',
  delay = 0,
}: PanelProps) {
  return (
    <section
      className={`panel rise ${className}`}
      style={{ animationDelay: `${delay}ms` }}
    >
      <div className="panel-title">
        <span className="text-fg">{title}</span>
        {meta != null && (
          <span className="ml-auto tracking-normal text-fg-dim">{meta}</span>
        )}
      </div>
      {children}
    </section>
  )
}
