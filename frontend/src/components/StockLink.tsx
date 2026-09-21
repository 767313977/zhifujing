import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

/**
 * 个股链接 —— **凡是在表里/卡片里出现个股代码或名称的地方，都用它包一下**。
 *
 * 为什么专门抽一个组件：之前不少列表只有「代码」是链接、「名称」是个普通 span。
 * 用户看一只票先读的是名称，点上去却什么也不发生 —— 板块成分股、龙虎榜、
 * 自选股面板都这样，报上来就是「个股点不开、看不了日K」。
 * 把规则写进组件里，以后新加的列表不会再漏掉一个入口。
 *
 * 这一层只负责导航，**不碰 router 的其它状态**：列表页在 tr 上的 onClick
 * （记列表、供个股页 ← → 前后翻）靠事件冒泡照常触发，两件事互不干扰。
 * 反过来也别把整行做成链接 —— 那会和 Link 各推一次历史，退回来要按两下
 * （LimitTable 里记着这个坑）。
 */
export default function StockLink({
  code,
  children,
  className = 'text-fg',
  title,
}: {
  code: string
  /** 显示的文字。留空就显示代码本身 */
  children?: ReactNode
  /** 需要 `num`（等宽）或弱色时从调用处传，例如 `num text-fg-muted` */
  className?: string
  /** 悬停提示。梯队 chip 那种「一块内容里带细节」的地方靠它，链接上照样能显示 */
  title?: string
}) {
  return (
    <Link
      to={`/stock/${code}`}
      title={title}
      className={`transition-colors hover:text-accent ${className}`}
    >
      {children ?? code}
    </Link>
  )
}
