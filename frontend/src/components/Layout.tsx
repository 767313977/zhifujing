import type { ReactNode } from 'react'
import { NavLink } from 'react-router-dom'

const NAV = [
  { to: '/', label: '今日复盘', end: true },
  { to: '/sentiment', label: '情绪周期' },
  { to: '/sectors', label: '板块题材' },
  { to: '/limit-up', label: '涨停复盘' },
  { to: '/funds', label: '资金面' },
  { to: '/patterns', label: '形态选股' },
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
        {/*
          顶栏在窄屏折成两行：第一行品牌 + 导航，第二行操作区；md 以上回到一行。

          为什么不折不行：一行里「品牌 + 9 项导航 + 三四个控件」在 420px 里必然装不下，
          而导航是**唯一能被压缩的那个**（`overflow-x-auto` 让它的自动最小尺寸变成 0，
          别的都是 shrink-0 或内容宽），于是浏览器先把它压到 0 宽 —— 结果是导航整条
          不可见、工具栏把页面顶出 157px 的横向滚动条（实测）。

          ⚠️ 三个都是踩过的坑，改之前先看这里：

          1. 只给操作区加 `w-full` 而不开 `flex-wrap` 是没用的 —— 那只会把它压成
             100% 宽却仍挤在同一行。
          2. 反过来 `md:flex-nowrap` 是必要的：768~1265px 这段宽度下，一行内容的
             「基准宽度和」仍超过视口，不锁死的话桌面笔记本上顶栏也会莫名折行。
          3. **导航必须显式 `basis-0 grow`**。换行判据用的是 flex 基准尺寸，而
             `overflow-x-auto` 只改「最小尺寸」、不改基准尺寸 —— 不动它的话导航的
             基准宽仍是内容宽 677px，第一行永远放不下「品牌 + 导航」，于是折成
             **三行**（品牌 / 导航 / 操作区，实测 420px 下 139px 高）。`basis-0` 把它
             的基准宽压到 0，导航才会留在第一行、并且自己吃掉这一行的剩余宽度。

          ⚠️ 容器高度从固定的 `h-[52px]` 改成自适应（折行后要能变高），**高度因此下移到
          各子项**（品牌与导航各带 `h-[52px]`）；原来 nav 上的 `h-full` 在这里会失效
          —— 百分比高度要求父级有确定高度。
        */}
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-x-3 px-3 md:flex-nowrap md:gap-x-6 md:px-5">
          {/* 品牌：中文名做主标识，等宽拉丁字母做辅助标记。
              字号与字距是响应式的，当初按**7 个字的长站名**调过：照常规参数窄屏
              会从导航那里抢走约 80px 可视宽度（那一段导航本来就靠横滑，再挤就
              只剩百来 px），所以窄屏收成 13px/0.05em、宽屏才展开成 15px/0.1em。
              站名已换成 3 个字的「致富经」，这套参数没回退 —— 现在富余很多，
              窄屏 13px 看着反而更稳。

              ⚠️ 拉丁标记的断点是 **xl（1280）不是 lg（1024）**：这一项本身就有几十 px
              宽，而 1024~1279 正好是导航最紧张的区间（那一段导航本来就显示不全）。
              长站名时期的实测是：按 1024 显示，等于用「一个纯装饰」换掉整整一个
              导航项（导航从 6/9 掉到 5/9）。**这些数是长站名时量的** —— 站名缩短后
              品牌窄了一大截、导航随之变宽松，但没重新量过。要动这个断点，先在
              1024 / 1280 两档各量一次再改。1280 以上富余够，随便显示。 */}
          <div className="flex h-[52px] shrink-0 items-baseline gap-2">
            <span className="text-[13px] font-semibold tracking-[0.05em] text-fg whitespace-nowrap md:text-[15px] md:tracking-[0.1em]">
              致富经
            </span>
            <span className="num hidden text-[11px] tracking-[0.28em] text-fg-dim xl:inline">
              ZHIFUJING
            </span>
          </div>

          <span className="h-4 w-px shrink-0 bg-line" />

          {/*
            ⚠️ 导航在 lg~xl（1024~1279）这三档是**故意调紧的**，别当成写错：
            当初 7 字站名时量到的是 —— 固定项（品牌 115.5 + 分隔线 1 + 操作区 258 +
            三个 24px 间隙）占掉 446.5px，1024 下 nav 只剩 527.5px，而 9 项按常规
            密度要 677px。调紧后（字号 13→12、左右内边距 12→6、项间距 4→0）内容降到
            504px，正好放下。xl（1280）起富余够（nav 689 ≥ 677），所以**恢复常规密度**
            —— 主力宽度不受影响。

            站名换成 3 个字的「致富经」后品牌窄了、nav 随之变宽松，所以上面这组数是
            **偏保守**的；但没重新量过，别把它当成可以用掉的余量。

            代价要认：那一段的点击热区只有 6px 内边距、相邻项之间只隔 12px，比别处挤。
            将来若增减导航项，第一件事是把这里的等式重新量一遍（项宽 = 字数×字号 + 内边距×2）。
          */}
          <nav className="no-scrollbar flex h-[52px] min-w-0 grow basis-0 items-stretch gap-1 overflow-x-auto overflow-y-hidden lg:gap-0 xl:gap-1">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  [
                    'relative flex items-center px-3 text-[13px] whitespace-nowrap transition-colors',
                    // 只在 1024~1279 收紧，1280 起覆盖回常规值（见上面那段说明）
                    'lg:px-1.5 lg:text-[12px] xl:px-3 xl:text-[13px]',
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

          {/* 操作区。窄屏 `w-full` 把它挤到第二行并占满整行，md 以上恢复成
              「靠右、宽度随内容」。

              不用 `ml-auto`：导航那边的 `grow` 已经吃满剩余空间、自然把这一块顶到右边，
              再加 auto margin 是空操作（实测 computed `margin-left: 0px`）。

              两道兜底都是为了「宁可内部横滑，也不压扁控件、更不顶破页面」：
              - `*:shrink-0`：没有它的话子控件会被压到 min-content —— 实测 360px 下
                口径段选被压成 85px、两个按钮的文字各折成两行，而 `overflow-x-auto`
                根本没触发（控件还能缩，就不算溢出）。
              - `overflow-x-auto`：真到了放不下的宽度（360px 下三个控件 379px > 可用
                336px），让它在这一条里横滑。不要 `justify-end` —— 内容超宽时右对齐
                会把左端（口径切换）挤出可视区，要从左往右滑才看得到。

              ⚠️ 往这里塞控件是有代价的：**导航是顶栏里唯一可收缩的元素**，所以操作区
              每宽 1px 都从导航身上扣 —— 1080p 以下很容易扣到导航要横滑才够得着最后几项。
              实测过一次：板块题材页的操作区曾经有三个控件（口径 + 日期 + 走势窗口，379px），
              1280px 下导航只剩 568px、只显示 7/9 项；把只管图表的那一个挪进图表 header
              之后回到 689px，9 项全展开。**判断标准是「这是不是全局切换」**：口径、日期
              是（整页都跟着变），图表窗口不是（它只影响那一两张图）。 */}
          <div className="no-scrollbar flex w-full shrink-0 items-center gap-3 overflow-x-auto pb-2 *:shrink-0 md:w-auto md:pb-0">
            {toolbar}
          </div>
        </div>
      </header>

      {/* 内边距与顶栏保持一致：两边不一样时窄屏下能看到内容与顶栏左边缘差 8px */}
      <main className="mx-auto max-w-[1600px] px-3 py-5 md:px-5">{children}</main>
    </div>
  )
}
