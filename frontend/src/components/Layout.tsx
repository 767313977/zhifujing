import type { ReactNode } from 'react'
import { NavLink } from 'react-router-dom'
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
  { to: '/sectors', label: '板块题材' },
  { to: '/limit-up', label: '涨停复盘' },
  { to: '/funds', label: '资金面' },
  // ⚠️ `end: true` 是必须的：下面 `胜率跟踪` 挂在 `/patterns/track` 下，不写的话
  // 站在跟踪页时「形态选股」也会一起高亮（NavLink 默认按前缀匹配）
  { to: '/patterns', label: '形态选股', end: true },
  { to: '/patterns/track', label: '胜率跟踪' },
  { to: '/watchlist', label: '自选股' },
  // 悟道之路（2026-10-08 用户要求加在「自选股」之后）。
  // ⚠️ 一开始做成了**指向 GitHub 仓库的外链**，用户随即反馈「点进去还是跳转」——
  // 他要的是站内的选股池页面。现在判定逻辑已经移植进来（`services/patterns.py` 的
  // `wudao_*` / `pile_wash_*` / `huabao_early`），所以这里通向站内 `/wudao`；
  // 仓库地址挪到那个页面里当「出处」链接。别再改回外链。
  { to: '/wudao', label: '悟道之路' },
  // 个股分析（2026-10-09 用户要求「单独做个页面，放在悟道之路的右侧」）：
  // 就是把原型那个「查票分析」框搬进站内 —— 输代码 / 名称 / 拼音首字母，出阶段判定。
  { to: '/stock-analysis', label: '个股分析' },
  { to: '/settings', label: '数据管理' },
]

interface LayoutProps {
  children: ReactNode
  /** 顶栏右侧的操作区，由各页面自行注入（日期切换、刷新等） */
  toolbar?: ReactNode
}

export default function Layout({ children, toolbar }: LayoutProps) {
  const { me } = useAuth()
  // 「数据管理」只给管理员看：那一页能触发采集（烧 iFinD 配额），还会显示邀请码与
  // 会员名单。前端隐藏只是体面，真正的拦截在后端（/api/admin/* 一律 403）。
  // 会员少一项，因此导航比下面注释里量过的 9 项还宽松一点。
  const items = me?.is_admin ? NAV : NAV.filter((item) => item.to !== '/settings')

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-line bg-ink-950/95 backdrop-blur-sm">
        {/*
          顶栏折成两行的断点是 **xl（1280）**：以下两行（第一行品牌 + 导航，第二行
          操作区），1280 及以上回到一行。

          ⚠️ 这个断点 2026-09-28 从 md（768）挪到了 xl —— 因为顶栏最右端多了账号区
          （当时是 `用户名 · 改密码 · 退出`，约 169px）。实测（headless Chrome + CDP 设视口、
          落地页 `/`、工具栏是最宽的那一档）：

          （2026-09-30：「改密码」已改成「账号」（见 UserMenu），账号区**窄了约 12px** ——
          下面这些数是改前量的，仍然成立，而且现在是**偏保守**的一侧。）
            · 一行布局下 1024 时导航可用 422px、内容要 516px（缺 94px）；
              1280 时可用 580px、内容要 690px（缺 110px）—— 最后 1~2 项被裁掉，
              而导航挂的是 `.no-scrollbar`，用户看不见任何「还能横滑」的提示。
            · 折成两行后导航独占第一行，1024 下可用约 774px > 690px，宽松。
          所以「一行放不下就折行」这件事在 1024~1279 也必须成立。

          为什么不折不行：一行里「品牌 + 9 项导航 + 三四个控件」在 420px 里必然装不下，
          而导航是**唯一能被压缩的那个**（`overflow-x-auto` 让它的自动最小尺寸变成 0，
          别的都是 shrink-0 或内容宽），于是浏览器先把它压到 0 宽 —— 结果是导航整条
          不可见、工具栏把页面顶出 157px 的横向滚动条（实测）。

          ⚠️ 三个都是踩过的坑，改之前先看这里：

          1. 只给操作区加 `w-full` 而不开 `flex-wrap` 是没用的 —— 那只会把它压成
             100% 宽却仍挤在同一行。
          2. `flex-nowrap` 现在挂在 **xl** 上（原来是 md）：1280 以上要锁死成一行，
             否则「品牌 + 导航 + 操作区」的基准宽度和也会让笔记本上莫名折行。
             1280 以下**必须允许折行**，否则操作区那 `w-full` 不起作用。
          3. **导航必须显式 `basis-0 grow`**。换行判据用的是 flex 基准尺寸，而
             `overflow-x-auto` 只改「最小尺寸」、不改基准尺寸 —— 不动它的话导航的
             基准宽仍是内容宽 677px，第一行永远放不下「品牌 + 导航」，于是折成
             **三行**（品牌 / 导航 / 操作区，实测 420px 下 139px 高）。`basis-0` 把它
             的基准宽压到 0，导航才会留在第一行、并且自己吃掉这一行的剩余宽度。

          ⚠️ 容器高度从固定的 `h-[52px]` 改成自适应（折行后要能变高），**高度因此下移到
          各子项**（品牌与导航各带 `h-[52px]`）；原来 nav 上的 `h-full` 在这里会失效
          —— 百分比高度要求父级有确定高度。
        */}
        <div className="mx-auto flex max-w-shell flex-wrap items-center gap-x-3 px-3 md:px-5 xl:flex-nowrap xl:gap-x-6">
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
            <span className="text-[14px] font-semibold tracking-[0.05em] text-fg whitespace-nowrap md:text-[15px] md:tracking-[0.1em]">
              致富经
            </span>
            <span className="num hidden text-[12px] tracking-[0.28em] text-fg-dim xl:inline">
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

            ⚠️ 2026-09-25 全站字号 +1px（用户「字体调大一点」）时**唯独导航没动**：
            上面那组等式是逐 px 量过的，9 项各 +1px 等于多要 25~30px，1024 与 1280
            两档会从「正好放下」变成「必须横滑」。导航本身是等宽字重里最不需要放大的
            （它是图标级别的短词），宁可比正文小一档，也不动这组等式。

            ✅ **2026-09-27 加了第 9 项「胜率跟踪」后实测（headless Chrome + CDP 设视口）**：
            1024×800 → nav.scrollWidth/clientWidth = **664/664**、documentElement = 1014/1014；
            1280×900 → **823/823**、1270/1270。两档都没有溢出、9 项文字完整，整页无横向
            滚动条。也就是说站名从 7 字缩到 3 字（「致富经」）省下来的余量足够再放一项，
            上面那组偏保守的等式可以作废 —— 但**再加第 10 项之前仍要重量一次**。

            ✅ **2026-10-08 加第 10 项「悟道之路」（外链）时又量了一次** —— 量法换了但更简单：
            把顶栏复刻成一个静态页（CSS 直接用 `dist` 的构建产物），用
            `msedge --headless=new --window-size=W,800 --dump-dom` 跑，页内脚本把
            `nav` 的左右边界、内容右沿写进 `data-result` 再读出来。
            （`--window-size` 要比目标视口大 30px：Edge 的窗口边不算在 innerWidth 里。）

            | 视口 | 导航可用 | 9 项内容 | 10 项内容 |
            | --- | --- | --- | --- |
            | 1024（折两行） | 910 | 517 | **589** |
            | 1280（一行） | 762 | 516 | **588**（余 174） |
            | 1536（一行） | 1018 | 516 | **588** |

            一项 4 字的成本正好是 `4×12 + 6×2 = 60px`（与 112 行那条等式对得上）；
            当时「悟道之路」还是外链、末尾带个 `↗`（10px + 2px 边距）所以再多 12px。
            **它后来改成站内路由、`↗` 去掉了**，所以是 577 / 576 / 576。

            ✅ **2026-10-09 加第 11 项「个股分析」**（也在「悟道之路」右边）：它也是 4 个字，
            按上面那条等式**再 +60px** → 约 637 / 636 / 636。三档的可用宽度是 910 / 762 / 1018，
            余量仍有 273 / 126 / 382 —— **没有溢出，这一版是推算的、没有重新上 headless 量**；
            下次真正挤不下时再按 135 行那套办法实测，并记得先怀疑操作区而不是导航。
            操作区按最宽那一档（258px）建模；真到了再挤不下的时候，先动的是这里的
            密度（字号/内边距），别去改 `xl:flex-nowrap`。
          */}
          <nav className="no-scrollbar flex h-[52px] min-w-0 grow basis-0 items-stretch gap-1 overflow-x-auto overflow-y-hidden lg:gap-0">
            {items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  [
                    'relative flex items-center px-3 text-[13px] whitespace-nowrap transition-colors',
                    // 1024 起收紧。**不再在 xl 恢复常规密度**（2026-09-28 改）：
                    // 顶栏多了右上角账号区，实测按 13px 算会被裁。12px + px-1.5 下
                    // 10 项（2026-10-08）仍有余量，见上面那段实测数字。
                    'lg:px-1.5 lg:text-[12px]',
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

          {/* 操作区。xl 以下 `w-full` 把它挤到第二行并占满整行，1280 以上恢复成
              「靠右、宽度随内容」（断点 2026-09-28 从 md 挪到 xl，理由见上面那段：
              账号区占了 169px 之后 1024~1279 一行放不下）。

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
              是（整页都跟着变），图表窗口不是（它只影响那一两张图）。
              2026-09-28 就是被这条咬到：右上角账号区（169px）加进来之后导航开始被裁，
              于是把折行断点从 md 挪到 xl（见上）。 */}
          <div className="no-scrollbar flex w-full shrink-0 items-center gap-3 overflow-x-auto pb-2 *:shrink-0 xl:w-auto xl:pb-0">
            {toolbar}
            {/* 账号区放**最右端**（即整页右上角）。它拼在操作区里而不是单开一块：
                顶栏的剩余宽度全归导航，单开一块等于再切一刀。 */}
            <UserMenu />
          </div>
        </div>
      </header>

      {/* 内边距与顶栏保持一致：两边不一样时窄屏下能看到内容与顶栏左边缘差 8px。
          宽度上限用同一个 `max-w-shell`（见 index.css 里的说明），否则大屏上
          顶栏会比内容宽出一截。 */}
      <main className="mx-auto max-w-shell px-3 py-5 md:px-5">{children}</main>

      {/* 页脚抽成了组件：登录页也要渲染备案号（见 Footer 的说明） */}
      <Footer />
    </div>
  )
}
