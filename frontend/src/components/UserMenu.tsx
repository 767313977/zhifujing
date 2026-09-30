import { Link } from 'react-router-dom'
import { useAuth } from '../lib/auth'

/**
 * 顶栏右上角的账号区：`用户名 · 账号 · 退出`。
 *
 * 2026-09-28 从页脚挪过来的（原本的设计是「页脚零代价」，但用户看过之后要它更显眼）。
 * 代价要认：顶栏操作区是全站最挤的地方 —— 导航在 1024px 下是「刚好放下、零余量」，
 * 这里每宽 1px 都从导航身上扣，扣到不够时导航退化成横滑（见 Layout 里那段量过的注释，
 * 以及 §6.1）。所以三个元素都压到 12px、间距收到 8px。
 *
 * ⚠️ 链接文字 2026-09-30 从「改密码」改成「**账号**」：那一页现在不只改密码（还带了
 * 「联系站长」的二维码），叫「改密码」会让联系方式根本没人找得到。顺带还短一个字。
 *
 * ⚠️ 登录页不渲染 Layout，所以那一页没有这块 —— 没登录时它本来也没意义。
 * ⚠️ `ml-auto` 不能删：xl 以下顶栏折成两行，账号区落在第二行（和工具栏同一行）。
 * 那一行是 `w-full` 的 flex，如果没有东西吃掉剩余空间，它会**紧跟工具栏左对齐** ——
 * 实测 1024 下落在 x≈300–448，看着完全不像「右上角」。`ml-auto` 把剩余空间
 * 全吸到左边，于是贴着右边缘。单行布局（≥xl）时那一行是内容宽、没有剩余空间，
 * `ml-auto` 等于没写，不影响。
 */
export default function UserMenu() {
  const { me, logout } = useAuth()
  if (me === null) return null

  return (
    <span className="ml-auto flex shrink-0 items-center gap-2 text-[12px] text-fg-dim">
      <span className="h-4 w-px bg-line" />
      <span className="num" title={me.is_admin ? `${me.username}（管理员）` : me.username}>
        {me.username}
      </span>
      <span className="text-line">·</span>
      <Link to="/account" className="transition-colors hover:text-fg">
        账号
      </Link>
      <span className="text-line">·</span>
      <button
        type="button"
        onClick={() => void logout()}
        className="transition-colors hover:text-fg"
      >
        退出
      </button>
    </span>
  )
}
