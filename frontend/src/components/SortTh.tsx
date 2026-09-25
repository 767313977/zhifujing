import type { ReactNode } from 'react'
import type { SortDir } from '../lib/sort'

interface SortThProps {
  children: ReactNode
  /**
   * 给了才可点。不给就是普通表头 —— 「操作」「备注」这类列没有可比的值，
   * 挂上箭头会让人点了没反应。
   */
  sortKey?: string
  /** 当前排序列（来自 `useSort` 的返回值） */
  activeKey?: string | null
  /** 当前方向（来自 `useSort` 的返回值） */
  dir?: SortDir
  /** 来自 `useSort` 的返回值 */
  onToggle?: (key: string) => void
  /** 文字列传 'left'，与单元格的对齐保持一致 */
  align?: 'left' | 'right'
  /**
   * 额外类名。目前只有一处用途：窄屏隐藏列时传 `max-xl:hidden`
   * （调用方必须给 `th` 与 `td` 传**同一个**类，只藏一边会整列错位）。
   */
  className?: string
  /** 补充说明（如「近 20 日日均成交额」）。可排序时会叠加「点击排序」提示 */
  title?: string
}

/**
 * 可排序的 `<th>`。全站表头统一走这个组件，保证箭头样式与交互一致。
 *
 * **onClick 挂在整个 `<th>` 上，内层 button 只负责键盘可达与承载样式。**
 * 一开始是把 onClick 挂在 button 上、再用伪元素往外扩点击热区，但那只够
 * 扩到单元格内边距的边界 —— 「窄按钮 + 宽单元格」的组合（比如「领涨股」
 * 列头文字两三个字、单元格却有 120px）两侧仍留着几十像素的死区，点了没反应。
 * 挂在 th 上就没有这个问题：整格都是热区。
 *
 * 内层 button **不再自己挂 onClick**：键盘用户 tab 到它、按 Enter 时浏览器会
 * 派发 click 事件，冒泡到 th 上照样触发。两处都挂的话一次点击会翻转两次，
 * 表现成「点了没反应」。
 *
 * 指示器（⇅ / ▲ / ▼）未排序时**只在悬停时出现** —— 常驻的话每个列头都挂一个
 * 箭头，表头会变成一排噪音，反而看不出当前排的是哪一列。
 */
export default function SortTh({
  children,
  sortKey,
  activeKey = null,
  dir = 'desc',
  onToggle,
  align,
  className,
  title,
}: SortThProps) {
  const base = [align === 'left' ? '!text-left' : '', className].filter(Boolean).join(' ')
  const sortable = sortKey != null && onToggle != null

  if (!sortable) {
    return (
      <th className={base || undefined} title={title}>
        {children}
      </th>
    )
  }

  const active = sortKey === activeKey
  return (
    <th
      className={[base, 'sortable', active ? 'is-sorted' : ''].filter(Boolean).join(' ')}
      // aria-sort 属于列头而不是按钮，屏幕阅读器靠它念「已按此列降序排列」
      aria-sort={active ? (dir === 'asc' ? 'ascending' : 'descending') : 'none'}
      onClick={() => onToggle(sortKey)}
    >
      <button
        type="button"
        title={title ? `${title}（点击排序）` : '点击排序'}
        className="sort-th"
      >
        {children}
        <span className="sort-th-mark">{active ? (dir === 'asc' ? '▲' : '▼') : '⇅'}</span>
      </button>
    </th>
  )
}
