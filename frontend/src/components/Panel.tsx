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
        {/* 右上角是辅助信息（数量、口径），两处都要显式写：字号不写会跟着
            `.panel-title` 变成 15px，字重不写会继承它的 600 —— 那样这段就和
            一级标题抢视线了。12px + 常规字重 + 弱化色，才是「辅助注释」那一档。 */}
        {meta != null && (
          <span className="ml-auto text-[12px] font-normal tracking-normal text-fg-dim">
            {meta}
          </span>
        )}
      </div>
      {children}
    </section>
  )
}
