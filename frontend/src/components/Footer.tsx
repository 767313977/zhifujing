import { Link } from 'react-router-dom'
import { useAuth } from '../lib/auth'

/**
 * 页脚：左边的备案号 + 右边的账号区（用户名 / 改密码 / 退出）。
 *
 * 为什么账号区放这里而不是顶栏：顶栏是全站最挤的地方（见 Layout 里那段说明，
 * 1024px 下导航已经 664/664、一点余量都没有），为一个人名去动它不划算；
 * 而退出登录这种一个月用不上一次的操作，藏在页脚完全够用。
 *
 * 登录页也渲染它 —— 登录页是「没登录就进不来」的唯一例外，而**管局要求备案号
 * 挂在站上**，用户第一眼看到的就是登录页，所以这一块两处都得有。
 */
export default function Footer() {
  const { me, logout } = useAuth()

  return (
    <footer className="mx-auto flex max-w-shell flex-wrap items-center gap-x-3 gap-y-1 px-3 pt-2 pb-6 text-[12px] md:px-5">
      <a
        href="https://beian.miit.gov.cn/"
        target="_blank"
        rel="noreferrer"
        className="num text-fg-dim transition-colors hover:text-fg-muted"
      >
        陕ICP备2026027279号
      </a>

      {me && (
        <span className="ml-auto flex items-center gap-2 text-fg-dim">
          <span className="num" title={`${me.username}${me.is_admin ? '（管理员）' : ''}`}>
            {me.username}
          </span>
          <span className="text-line">·</span>
          <Link to="/account" className="transition-colors hover:text-fg-muted">
            改密码
          </Link>
          <span className="text-line">·</span>
          <button
            type="button"
            onClick={() => void logout()}
            className="transition-colors hover:text-fg-muted"
          >
            退出
          </button>
        </span>
      )}
    </footer>
  )
}
