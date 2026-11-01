import type { ReactNode, Ref } from 'react'
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

/**
 * 固定项（规范 v1 §4.1，2026-11-01）：**顺序是用户点名的，不要调换**。
 *
 * 7 个入口全是一天里反复用的页面：复盘 → 盘前 → 涨停 / 板块 → 选股名单 → 自选。
 * 其余入口按领域收进 `GROUPS`，**一个都没删**，URL 与书签全部不变。
 */
const PINNED: NavItem[] = [
  { to: '/', label: '今日复盘', end: true },
  // 盘前 / 开盘（FEAT-PREMARKET，2026-10-31）：服务 9:15–10:00 的短线习惯，
  // 纯读库（昨日涨停今开 + 悟道名单 + 自选），不做分时盯盘。
  { to: '/premarket', label: '盘前' },
  { to: '/limit-up', label: '涨停复盘' },
  { to: '/sectors', label: '板块题材' },
  // ⚠️ `end: true` 是必须的：`胜率跟踪` 挂在 `/patterns/track` 下（现在收在「选股」组里），
  // 不写的话站在跟踪页时「形态选股」也会一起高亮（NavLink 默认按前缀匹配）
  { to: '/patterns', label: '形态选股', end: true },
  // 悟道之路（2026-10-08 用户要求）：判定逻辑已移植进站内，别再改回 GitHub 外链。
  { to: '/wudao', label: '悟道之路' },
  { to: '/watchlist', label: '自选股' },
]

/**
 * 分组（规范 v1 §7.4）：复盘 / 选股 / 交易 / 个股 / 系统。
 *
 * 每个入口**只出现一次**（固定项与分组不重复），点开是**下拉菜单**而不是 hover ——
 * 鼠标在顶栏横向扫过时不该弹出一堆菜单（2026-11-01 规范要求）。
 */
const GROUPS: { key: string; label: string; items: NavItem[] }[] = [
  {
    key: 'review',
    label: '复盘',
    items: [
      { to: '/sentiment', label: '情绪周期' },
      { to: '/auction', label: '竞价强度' },
      { to: '/funds', label: '资金面' },
      { to: '/valuation', label: '估值分位' },
    ],
  },
  {
    key: 'pick',
    label: '选股',
    items: [
      { to: '/pools', label: '选股池' },
      { to: '/patterns/track', label: '胜率跟踪' },
      { to: '/cb', label: '可转债' },
      { to: '/etf', label: 'ETF' },
    ],
  },
  {
    key: 'trade',
    label: '交易',
    items: [
      { to: '/journal', label: '交易日志' },
      { to: '/plans', label: '明日计划' },
      { to: '/positions', label: '持仓' },
    ],
  },
  {
    key: 'stock',
    label: '个股',
    items: [
      { to: '/anomaly', label: '个股异动' },
      { to: '/stock-analysis', label: '个股分析' },
    ],
  },
  {
    // 「系统」组**只给管理员**：数据管理页能触发采集（烧 iFinD 配额），还显示邀请码与
    // 会员名单。前端隐藏只是体面，真正的拦截在后端（/api/admin/* 一律 403）。
    key: 'system',
    label: '系统',
    items: [{ to: '/settings', label: '数据管理' }],
  },
]

/**
 * 路由是否命中该入口。
 *
 * 与 NavLink 的匹配规则保持一致（`end` 用全等、其余用「相等或子路径」）——
 * 下拉按钮的「当前分组高亮」用它，不然会出现「菜单里亮着、按钮不亮」。
 */
function isActivePath(pathname: string, item: NavItem): boolean {
  if (item.end) return pathname === item.to
  return pathname === item.to || pathname.startsWith(`${item.to}/`)
}

/**
 * 顶栏导航（规范 v1 §4.1，2026-11-01 改版）。
 *
 * 结构：7 个固定项平铺 + 5 个分组下拉。历史背景：
 * 2026-10-23 曾做过「主项 + 更多 ▾」收纳，2026-10-24 用户要求「不要压缩、全部平铺」；
 * 现在入口涨到 21 个，规范 v1 重新要求按领域收组 —— 这次是**有明确分组**的收纳，
 * 不是把东西塞进「更多」，所以不违背当时那条「别多一次点击」的诉求：
 * 常用 7 个仍然一点就到，其余按领域最多多一次点击。
 *
 * ⚠️ 分组按钮**必须放在滚动容器之外**：固定项那条 nav 是 `overflow-x-auto`（窄屏兜底），
 * 下拉菜单挂在里面会被裁掉。
 */

/**
 * 一个固定项。下划线贴容器底边 —— 往外溢出会逼出竖向滚动条（2026-09 踩过）。
 */
function NavEntry({ item }: { item: NavItem }) {
  return (
    <NavLink
      to={item.to}
      end={item.end}
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

/** 分组下拉。点击展开（不是 hover），Esc / 点外部收起。 */
function GroupEntry({
  group,
  open,
  onToggle,
}: {
  group: { key: string; label: string; items: NavItem[] }
  open: boolean
  onToggle: () => void
}) {
  const { pathname } = useLocation()
  const active = group.items.some((item) => isActivePath(pathname, item))
  return (
    <div className="relative flex items-stretch">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={`nav-group-${group.key}`}
        onClick={onToggle}
        className={[
          'relative flex items-center gap-1 px-3 text-[13px] whitespace-nowrap transition-colors',
          'lg:px-1.5 lg:text-[12px]',
          active || open ? 'text-fg' : 'text-fg-muted hover:text-fg',
        ].join(' ')}
      >
        {group.label}
        <span
          aria-hidden
          className={`text-[9px] transition-transform ${open ? 'rotate-180' : ''}`}
        >
          ▾
        </span>
        {active && (
          <span className="absolute inset-x-2 bottom-0 h-[2px] bg-accent wipe" aria-hidden />
        )}
      </button>
      {open && (
        <div
          id={`nav-group-${group.key}`}
          role="menu"
          aria-label={`${group.label}菜单`}
          className="absolute left-0 top-full z-30 mt-0.5 min-w-[132px] border border-line bg-ink-900 py-1 rounded-sm shadow-pop"
        >
          {group.items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              role="menuitem"
              className={({ isActive }) =>
                [
                  'block px-3 py-1.5 text-[13px] whitespace-nowrap transition-colors',
                  isActive
                    ? 'text-accent'
                    : 'text-fg-muted hover:bg-ink-850 hover:text-fg',
                ].join(' ')
              }
            >
              {item.label}
            </NavLink>
          ))}
        </div>
      )}
    </div>
  )
}

/** 抽屉里的一个入口（手机 / 平板 <1024）。 */
function DrawerLink({ item, itemRef }: { item: NavItem; itemRef?: Ref<HTMLAnchorElement> }) {
  return (
    <NavLink
      to={item.to}
      end={item.end}
      ref={itemRef}
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
  const groups = me?.is_admin ? GROUPS : GROUPS.filter((group) => group.key !== 'system')

  const [drawerOpen, setDrawerOpen] = useState(false)
  const drawerFirstLinkRef = useRef<HTMLAnchorElement>(null)

  // 桌面端的分组下拉：同一时间只开一个（点击切换，不 hover）
  const [openGroup, setOpenGroup] = useState<string | null>(null)
  const groupsRef = useRef<HTMLDivElement>(null)

  // 换页即收起抽屉与下拉（包括点菜单项、用 ← → 切个股）
  useEffect(() => {
    setDrawerOpen(false)
    setOpenGroup(null)
  }, [location.pathname, location.search])

  // 下拉的点外部关闭 + Esc 关闭。用 mousedown 而不是 click：
  // click 要等 mouseup，按钮上的第二次点击会先被这个监听判成「外部」再触发 toggle，
  // 表现为「点不关」。
  useEffect(() => {
    if (!openGroup) return
    const onPointerDown = (event: MouseEvent) => {
      if (!groupsRef.current?.contains(event.target as Node)) setOpenGroup(null)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpenGroup(null)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [openGroup])

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
          折行断点是 xl（1280）：以下两行（第一行品牌 + 导航，第二行操作区），
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
            {/* 规范 v1：ZHIFUJING 只在 ≥1536（2xl）出现，1280–1535 把宽度让给入口 */}
            <span className="num hidden text-[12px] tracking-[0.28em] text-fg-dim 2xl:inline">
              ZHIFUJING
            </span>
          </div>

          <span className="hidden h-4 w-px shrink-0 bg-line lg:block" />

          {/* 桌面/平板（≥1024）：7 个固定项平铺；装不下就横向滚动（见上面那段说明） */}
          <nav
            aria-label="主导航"
            className="hidden h-[52px] min-w-0 grow basis-0 items-stretch gap-1 overflow-x-auto overflow-y-hidden lg:flex lg:gap-0"
          >
            {PINNED.map((item) => (
              <NavEntry key={item.to} item={item} />
            ))}
          </nav>

          {/* 分组入口：**在滚动容器外**，否则下拉会被 overflow 裁掉 */}
          <div
            ref={groupsRef}
            className="hidden h-[52px] shrink-0 items-stretch lg:flex"
            aria-label="分组导航"
          >
            {groups.map((group) => (
              <GroupEntry
                key={group.key}
                group={group}
                open={openGroup === group.key}
                onToggle={() =>
                  setOpenGroup((current) => (current === group.key ? null : group.key))
                }
              />
            ))}
          </div>

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

      {/* 手机/窄屏抽屉：固定项 + 按组平铺（管理员多一组「系统」） */}
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
              {/* 固定项：不写小标题，它们是「一眼就要点」的那批 */}
              <div className="grid grid-cols-2 gap-1">
                {PINNED.map((item, index) => (
                  <DrawerLink
                    key={item.to}
                    item={item}
                    itemRef={index === 0 ? drawerFirstLinkRef : undefined}
                  />
                ))}
              </div>
              {groups.map((group) => (
                <div key={group.key} className="mt-3">
                  <div className="px-1 pb-1 text-[12px] tracking-[0.08em] text-fg-dim">
                    {group.label}
                  </div>
                  <div className="grid grid-cols-2 gap-1">
                    {group.items.map((item) => (
                      <DrawerLink key={item.to} item={item} />
                    ))}
                  </div>
                </div>
              ))}
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
