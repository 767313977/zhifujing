import type { ReactNode } from 'react'
import { NavLink } from 'react-router-dom'

const NAV = [
  { to: '/', label: '今日复盘', end: true },
  { to: '/sentiment', label: '情绪周期' },
  { to: '/sectors', label: '板块题材' },
  { to: '/limit-up', label: '涨停复盘' },
  { to: '/screener', label: '选股器' },
  { to: '/watchlist', label: '自选股' },
  { to: '/settings', label: '数据管理' },
]

interface LayoutProps {
  children: ReactNode
  /** 顶栏右侧的操作区，由各页面自行注入（日期切换、刷新等） */
  toolbar?: ReactNode
}

export default function Layout({ children, toolbar }: LayoutProps) {
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-line bg-ink-950/95 backdrop-blur-sm">
        <div className="mx-auto flex h-[52px] max-w-[1600px] items-center gap-6 px-5">
          {/* 品牌：中文名做主标识，等宽拉丁字母做辅助标记 */}
          <div className="flex shrink-0 items-baseline gap-2">
            <span className="text-[15px] font-semibold tracking-[0.2em] text-fg">复盘</span>
            <span className="num text-[10px] tracking-[0.28em] text-fg-dim">FUPAN</span>
          </div>

          <span className="h-4 w-px bg-line" />

          <nav className="no-scrollbar flex h-full items-stretch gap-1 overflow-x-auto overflow-y-hidden">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  [
                    'relative flex items-center px-3 text-[13px] whitespace-nowrap transition-colors',
                    isActive ? 'text-fg' : 'text-fg-muted hover:text-fg',
                  ].join(' ')
                }
              >
                {({ isActive }) => (
                  <>
                    {item.label}
                    {/* 下划线贴容器底边，不再向外溢出，否则会逼出竖向滚动条 */}
                    {isActive && (
                      <span className="absolute inset-x-2 bottom-0 h-[2px] bg-accent wipe" />
                    )}
                  </>
                )}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex shrink-0 items-center gap-3">{toolbar}</div>
        </div>
      </header>

      <main className="mx-auto max-w-[1600px] px-5 py-5">{children}</main>
    </div>
  )
}
