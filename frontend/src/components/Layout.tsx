import type { ReactNode } from 'react'
import { useEffect, useRef, useState } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { useAuth } from '../lib/auth'
import Footer from './Footer'
import UserMenu from './UserMenu'

interface NavItem {
  to: string
  label: string
  /** 站内路由才有：`end` 让前缀匹配不误高亮（见下面形态选股那条的说明） */
  end?: boolean
}

const NAV: NavItem[] = [
  { to: '/', label: '今日复盘', end: true },
  { to: '/sentiment', label: '情绪周期' },
  // 估值分位（2026-10-23 用户要求）：核心指数 PE/PB 的十年分位与估值温度
  { to: '/valuation', label: '估值分位' },
  // 相似日（2026-10-18 清单 #4）：用情绪向量找历史最像的几天。
  { to: '/similar', label: '相似日' },
  { to: '/sectors', label: '板块题材' },
  // 竞价强度（2026-10-11 用户要求「放在涨停复盘前边」）：集合竞价的涨幅 / 金额 /
  // 量比 / 换手，数据来自腾讯批量行情逐分钟采样（见后端 jobs/collect_auction.py）。
  { to: '/auction', label: '竞价强度' },
  { to: '/limit-up', label: '涨停复盘' },
  { to: '/funds', label: '资金面' },
  // ⚠️ `end: true` 是必须的：下面 `胜率跟踪` 挂在 `/patterns/track` 下，不写的话
  // 站在跟踪页时「形态选股」也会一起高亮（NavLink 默认按前缀匹配）
  { to: '/patterns', label: '形态选股', end: true },
  { to: '/patterns/track', label: '胜率跟踪' },
  { to: '/watchlist', label: '自选股' },
  // 交易日志（2026-10-18 清单 #1）：复盘自己的买卖，而不是只看市场。
  { to: '/journal', label: '交易日志' },
  // 明日计划（2026-10-18 清单 #2）：过 X 买、破 Y 放弃，收盘自动核对。
  { to: '/plans', label: '明日计划' },
  // 悟道之路（2026-10-08 用户要求加在「自选股」之后）。
  // ⚠️ 一开始做成了**指向 GitHub 仓库的外链**，用户随即反馈「点进去还是跳转」——
  // 他要的是站内的选股池页面。现在判定逻辑已经移植进来（`services/patterns.py` 的
  // `wudao_*` / `pile_wash_*` / `huabao_early`），所以这里通向站内 `/wudao`；
  // 仓库地址挪到那个页面里当「出处」链接。别再改回外链。
  { to: '/wudao', label: '悟道之路' },
  // 个股异动（2026-10-09 用户要求加在「悟道之路」与「个股分析」**之间**）：
  // 复刻原型的「豆包异动图谱」，数据全来自本站库（涨停池 + 本地日线），不外链。
  { to: '/anomaly', label: '个股异动' },
  // 个股分析（2026-10-09 用户要求「单独做个页面，放在悟道之路的右侧」）：
  // 就是把原型那个「查票分析」框搬进站内 —— 输代码 / 名称 / 拼音首字母，出阶段判定。
  { to: '/stock-analysis', label: '个股分析' },
  { to: '/settings', label: '数据管理' },
]

/**
 * 顶栏布局档位（2026-10-23 改，修复 16 项导航在 1024–1440 被裁切）：
 *
 * - `wide`（1280–1535）：主项 8 个 + 「更多 ▾」收纳剩下 8 个；
 * - `xwide`（≥1536）：主项 12 个 + 「更多 ▾」收纳剩下 4 个；
 * - `medium`（1024–1279）：顶栏**折两行**（断点仍是下文的 `xl`），导航独占第一行、
 *   约 900px 可用，主项 12 个 + 「更多」放得下；
 * - `mobile`（<1024）：整条导航收进汉堡抽屉，点开可看到**全部** 16 项。
 *
 * 为什么不再「全部平铺 + 横向滚动」：导航已涨到 16 项，实测 1280 下最窄的页面
 * （板块题材，操作区最宽）nav 只剩 585px、内容 924px —— 1280/1366/1440 分别被裁
 * 5/3/2 项，而 `.no-scrollbar` 把滚动条藏了，鼠标用户根本发现不了还能横滑
 * （2026-10-23 前端审计 P1）。改成「主项 + 更多」后：1280 下主项 8 个 456px +
 * 更多按钮约 60px = 516px < nav 可用 525px；1024 下 12 个 684px + 60px < 840px。
 *
 * ⚠️ 往 NAV 里加项后要重新量主项数：它是按**实测宽度**定的（单项 48~60px），
 * 菜单（更多/抽屉）本身对项数自适应、不会裁切。
 */

type Breakpoint = 'wide' | 'xwide' | 'medium' | 'mobile'

/** 与 Tailwind 断点保持一致：xl=1280、2xl=1536、lg=1024。 */
const WIDE_QUERY = '(min-width: 1280px)'
const XWIDE_QUERY = '(min-width: 1536px)'
const MEDIUM_QUERY = '(min-width: 1024px)'

function currentBreakpoint(): Breakpoint {
  if (typeof window === 'undefined') return 'wide'
  if (window.matchMedia(XWIDE_QUERY).matches) return 'xwide'
  if (window.matchMedia(WIDE_QUERY).matches) return 'wide'
  if (window.matchMedia(MEDIUM_QUERY).matches) return 'medium'
  return 'mobile'
}

/** 监听三个断点。用 matchMedia 而不是 resize 计数：方向键调窗口大小时也准。 */
function useBreakpoint(): Breakpoint {
  const [mode, setMode] = useState<Breakpoint>(currentBreakpoint)
  useEffect(() => {
    const queries = [WIDE_QUERY, XWIDE_QUERY, MEDIUM_QUERY].map((q) => window.matchMedia(q))
    const update = () => setMode(currentBreakpoint())
    // 首次 effect 再同步一次：兜住「模块加载后窗口尺寸立刻变化」
    update()
    queries.forEach((q) => q.addEventListener('change', update))
    return () => queries.forEach((q) => q.removeEventListener('change', update))
  }, [])
  return mode
}

/** 主项平铺几个（其余进「更多」；mobile 不渲染平铺导航）。 */
function primaryCountOf(mode: Breakpoint): number {
  if (mode === 'wide') return 8
  if (mode === 'xwide' || mode === 'medium') return 12
  return 0
}

function isItemActive(item: NavItem, pathname: string): boolean {
  if (item.end) return pathname === item.to
  return pathname === item.to || pathname.startsWith(`${item.to}/`)
}

/**
 * 一个导航项（桌面主项与「更多」里的项共用）。下划线贴容器底边 ——
 * 往外溢出会逼出竖向滚动条（2026-09 踩过）。
 */
function NavEntry({ item, onClick }: { item: NavItem; onClick?: () => void }) {
  return (
    <NavLink
      to={item.to}
      end={item.end}
      onClick={onClick}
      className={({ isActive }) =>
        [
          'relative flex items-center px-3 text-[13px] whitespace-nowrap transition-colors',
          // 1024 起收紧密度（与「导航必须显式 basis-0 grow」无关，纯省宽度）
          'lg:px-1.5 lg:text-[12px]',
          isActive ? 'text-fg' : 'text-fg-muted hover:text-fg',
        ].join(' ')
      }
    >
      {({ isActive }) => (
        <>
          {item.label}
          {isActive && (
            <span className="absolute inset-x-2 bottom-0 h-[2px] bg-accent wipe" aria-hidden />
          )}
        </>
      )}
    </NavLink>
  )
}

interface LayoutProps {
  children: ReactNode
  /** 顶栏右侧的操作区，由各页面自行注入（日期切换、刷新等） */
  toolbar?: ReactNode
}

export default function Layout({ children, toolbar }: LayoutProps) {
  const { me } = useAuth()
  const location = useLocation()
  const mode = useBreakpoint()
  // 「数据管理」只给管理员看：那一页能触发采集（烧 iFinD 配额），还会显示邀请码与
  // 会员名单。前端隐藏只是体面，真正的拦截在后端（/api/admin/* 一律 403）。
  const items = me?.is_admin ? NAV : NAV.filter((item) => item.to !== '/settings')
  const primaryCount = primaryCountOf(mode)
  const primaryItems = items.slice(0, primaryCount)
  const overflowItems = items.slice(primaryCount)
  const overflowActive = overflowItems.some((item) => isItemActive(item, location.pathname))

  const [moreOpen, setMoreOpen] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const moreRef = useRef<HTMLDivElement>(null)
  const drawerFirstLinkRef = useRef<HTMLAnchorElement>(null)

  // 换页即收起菜单（包括点「更多」里的项、用 ← → 切个股）
  useEffect(() => {
    setMoreOpen(false)
    setDrawerOpen(false)
  }, [location.pathname, location.search])

  // 「更多」：点外面或按 Esc 关掉
  useEffect(() => {
    if (!moreOpen) return
    const onPointerDown = (event: PointerEvent) => {
      if (moreRef.current && !moreRef.current.contains(event.target as Node)) setMoreOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMoreOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [moreOpen])

  // 手机抽屉：Esc 关闭、锁住背景滚动、打开时焦点进第一个链接
  useEffect(() => {
    if (!drawerOpen) return
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    drawerFirstLinkRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setDrawerOpen(false)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [drawerOpen])

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-line bg-ink-950/95 backdrop-blur-sm">
        {/*
          折行断点是 xl（1280）：以下两行（第一行品牌 + 导航 + 更多，第二行操作区），
          1280 及以上回到一行。这一条从 2026-09-28 起就是如此，保持不动。

          ⚠️ 操作区是顶栏里唯一「内容不可压缩」的一大块（日期下拉 + 账号区），
          所以它每宽 1px 都从导航身上扣。判断标准仍是「这是不是全局切换」：
          日期/口径是（整页跟着变），图表窗口不是。往这里塞控件前先想清楚。
        */}
        <div className="mx-auto flex max-w-shell flex-wrap items-center gap-x-3 px-3 md:px-5 xl:flex-nowrap xl:gap-x-6">
          {/* 品牌：中文名做主标识，等宽拉丁字母做辅助标记。 */}
          <div className="flex h-[52px] shrink-0 items-baseline gap-2">
            <span className="text-[14px] font-semibold tracking-[0.05em] text-fg whitespace-nowrap md:text-[15px] md:tracking-[0.1em]">
              致富经
            </span>
            <span className="num hidden text-[12px] tracking-[0.28em] text-fg-dim xl:inline">
              ZHIFUJING
            </span>
          </div>

          <span className="hidden h-4 w-px shrink-0 bg-line lg:block" />

          {/* 桌面/平板（≥1024）：主项平铺；剩下的进「更多 ▾」 */}
          <nav
            aria-label="主导航"
            className="hidden h-[52px] min-w-0 grow basis-0 items-stretch gap-1 overflow-x-auto overflow-y-hidden lg:flex lg:gap-0"
          >
            {primaryItems.map((item) => (
              <NavEntry key={item.to} item={item} />
            ))}
          </nav>

          {overflowItems.length > 0 && (
            <div ref={moreRef} className="relative hidden shrink-0 self-center lg:block">
              <button
                type="button"
                aria-haspopup="menu"
                aria-expanded={moreOpen}
                aria-controls="nav-more-menu"
                onClick={() => setMoreOpen((open) => !open)}
                className={[
                  'relative flex h-[52px] items-center gap-1 px-3 text-[13px] transition-colors lg:px-1.5 lg:text-[12px]',
                  overflowActive ? 'text-accent' : 'text-fg-muted hover:text-fg',
                ].join(' ')}
              >
                更多
                <span aria-hidden className="text-[9px] leading-none">
                  ▾
                </span>
                {/* 当前路由在「更多」里：与主项一致的贴底下划线，用户能看出「这页在这」 */}
                {overflowActive && (
                  <span className="absolute inset-x-2 bottom-0 h-[2px] bg-accent" aria-hidden />
                )}
              </button>
              {moreOpen && (
                <div
                  id="nav-more-menu"
                  role="menu"
                  aria-label="更多导航"
                  className="absolute top-full right-0 z-30 mt-1 grid w-[220px] grid-cols-1 gap-0.5 border border-line bg-ink-950 p-1.5 shadow-[0_12px_32px_rgba(15,23,42,0.18)]"
                >
                  {overflowItems.map((item) => (
                    <NavEntry key={item.to} item={item} onClick={() => setMoreOpen(false)} />
                  ))}
                </div>
              )}
            </div>
          )}

          {/* <1024：整条导航收进抽屉（汉堡）。放第一行右端，操作区仍占第二行 */}
          <button
            type="button"
            aria-label="打开导航菜单"
            aria-expanded={drawerOpen}
            aria-controls="nav-drawer"
            onClick={() => setDrawerOpen(true)}
            className="ml-auto flex h-[52px] w-9 shrink-0 items-center justify-center text-fg-muted transition-colors hover:text-fg lg:hidden"
          >
            <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden fill="none">
              <path
                d="M3 5h14M3 10h14M3 15h14"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="square"
              />
            </svg>
          </button>

          {/*
            操作区。xl 以下 `w-full` 把它挤到第二行并占满整行，1280 以上恢复成
            「靠右、宽度随内容」（断点 2026-09-28 从 md 挪到 xl）。

            两道兜底都是为了「宁可内部横滑，也不压扁控件、更不顶破页面」：
            `*:shrink-0` + `overflow-x-auto`（见 2026-09 的排查记录）。
          */}
          <div className="no-scrollbar flex w-full shrink-0 items-center gap-3 overflow-x-auto pb-2 *:shrink-0 xl:w-auto xl:pb-0">
            {toolbar}
            {/* 账号区放最右端（即整页右上角）。它拼在操作区里而不是单开一块：
                顶栏的剩余宽度全归导航，单开一块等于再切一刀。 */}
            <UserMenu />
          </div>
        </div>
      </header>

      {/* 手机/窄屏抽屉：全部导航项（管理员多一项「数据管理」） */}
      {drawerOpen && (
        <div
          className="fixed inset-0 z-40 lg:hidden"
          role="dialog"
          aria-modal="true"
          aria-label="导航菜单"
        >
          {/* 背景遮罩：点一下就关 */}
          <div
            className="absolute inset-0 bg-ink-950/60"
            onClick={() => setDrawerOpen(false)}
            aria-hidden
          />
          <div
            id="nav-drawer"
            className="absolute inset-y-0 right-0 flex w-[86%] max-w-[340px] flex-col border-l border-line bg-ink-950 shadow-[-12px_0_32px_rgba(15,23,42,0.2)]"
          >
            <div className="flex h-[52px] shrink-0 items-center justify-between border-b border-line px-4">
              <span className="text-[14px] font-semibold tracking-[0.05em] text-fg">导航</span>
              <button
                type="button"
                aria-label="关闭导航菜单"
                onClick={() => setDrawerOpen(false)}
                className="flex h-8 w-8 items-center justify-center text-fg-muted transition-colors hover:text-fg"
              >
                <span aria-hidden>✕</span>
              </button>
            </div>
            <nav aria-label="全部导航" className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
              <div className="grid grid-cols-2 gap-1">
                {items.map((item, index) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.end}
                    ref={index === 0 ? drawerFirstLinkRef : undefined}
                    className={({ isActive }) =>
                      [
                        'flex min-h-[42px] items-center border px-3 text-[14px] transition-colors',
                        isActive
                          ? 'border-accent/60 bg-accent/10 text-accent'
                          : 'border-line-soft text-fg-muted hover:border-fg-dim hover:text-fg',
                      ].join(' ')
                    }
                  >
                    {item.label}
                  </NavLink>
                ))}
              </div>
            </nav>
            <div className="border-t border-line-soft px-4 py-3 text-[12px] text-fg-dim">
              点右上角 ✕ 或遮罩关闭；关闭后回到原页面滚动位置
            </div>
          </div>
        </div>
      )}

      {/* 内边距与顶栏保持一致：两边不一样时窄屏下能看到内容与顶栏左边缘差 8px。
          宽度上限用同一个 `max-w-shell`（见 index.css 里的说明），否则大屏上
          顶栏会比内容宽出一截。 */}
      <main className="mx-auto max-w-shell px-3 py-5 md:px-5">{children}</main>

      {/* 页脚抽成了组件：登录页也要渲染备案号（见 Footer 的说明） */}
      <Footer />
    </div>
  )
}
