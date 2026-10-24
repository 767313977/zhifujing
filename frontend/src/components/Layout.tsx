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
 * 顶栏导航：**16 项全部平铺**，放不下就横向滚动。
 *
 * 2026-10-23 曾改成「主项 + 更多 ▾」收纳（理由是 1024–1440 下会被裁切，而滚动条
 * 被藏起来了）。2026-10-24 用户明确要求「不要压缩，能放得下」—— 他的屏幕放得下，
 * 宁可在窄屏上横向滚动，也不要多一次点击。所以这里回到全部平铺。
 *
 * ⚠️ 不要再加 `.no-scrollbar`：横滑是这项决定的兜底，滚动条必须看得见，
 * 否则又回到「用户不知道右边还有」的老问题。
 */

/**
 * 一个导航项。下划线贴容器底边 —— 往外溢出会逼出竖向滚动条（2026-09 踩过）。
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

interface LayoutProps {
  children: ReactNode
  /** 顶栏右侧的操作区，由各页面自行注入（日期切换、刷新等） */
  toolbar?: ReactNode
}

export default function Layout({ children, toolbar }: LayoutProps) {
  const { me } = useAuth()
  const location = useLocation()
  // 「数据管理」只给管理员看：那一页能触发采集（烧 iFinD 配额），还会显示邀请码与
  // 会员名单。前端隐藏只是体面，真正的拦截在后端（/api/admin/* 一律 403）。
  const items = me?.is_admin ? NAV : NAV.filter((item) => item.to !== '/settings')

  const [drawerOpen, setDrawerOpen] = useState(false)
  const drawerFirstLinkRef = useRef<HTMLAnchorElement>(null)

  // 换页即收起抽屉（包括点抽屉里的项、用 ← → 切个股）
  useEffect(() => {
    setDrawerOpen(false)
  }, [location.pathname, location.search])

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
            <span className="num hidden text-[12px] tracking-[0.28em] text-fg-dim xl:inline">
              ZHIFUJING
            </span>
          </div>

          <span className="hidden h-4 w-px shrink-0 bg-line lg:block" />

          {/* 桌面/平板（≥1024）：**全部平铺**；装不下就横向滚动（见上面那段说明） */}
          <nav
            aria-label="主导航"
            className="hidden h-[52px] min-w-0 grow basis-0 items-stretch gap-1 overflow-x-auto overflow-y-hidden lg:flex lg:gap-0"
          >
            {items.map((item) => (
              <NavEntry key={item.to} item={item} />
            ))}
          </nav>

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
