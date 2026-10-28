import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'

/**
 * 根级错误边界（2026-10-28 体检 P2）。
 *
 * **为什么必须有**：页面代码块是 `lazy()` 按需加载的，部署换哈希并清掉旧产物之后，
 * 已经打开旧标签页的用户再点任一没加载过的页面 → 动态 `import()` 404；或者任一页面
 * 渲染时抛 `TypeError`。`Suspense` 只接管 **pending**，`lazy` 的 rejection 会一路上抛，
 * 而 React 的默认行为是**卸载整棵树** —— `#root` 变空、纯白、没有任何提示，
 * 和 2026-09-26 那次白屏事故长得一模一样（那次是缓存指到已删的产物）。
 *
 * 兜住两种：① 渲染期异常；② 懒加载 promise 失败（React 19 会把它当作一次错误抛出，
 * 同样能被 `componentDidCatch` 接住）。堆栈只打 console，不画到页面上 —— 站点虽然是
 * 个人自用，但把内部路径暴露在界面上没有任何好处。
 */
interface ErrorBoundaryState {
  error: Error | null
}

export default class ErrorBoundary extends Component<
  { children: ReactNode },
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 保留控制台线索（线上排查靠它），但不要把堆栈渲染到页面上
    console.error('页面渲染失败：', error, info.componentStack)
  }

  private reload = () => {
    window.location.reload()
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <div className="mx-auto flex min-h-[60vh] max-w-xl flex-col items-center justify-center gap-3 px-6 text-center">
        <div className="text-[16px] font-semibold text-fg">页面加载失败</div>
        <div className="text-[13px] leading-relaxed text-fg-dim">
          多半是版本刚更新、浏览器还拿着旧的页面代码块（也可能是网络断了）。
          点下面的按钮重新加载即可；如果反复出现，清一下浏览器缓存再试。
        </div>
        <pre className="max-h-40 w-full overflow-auto rounded border border-line-soft bg-ink-850 px-3 py-2 text-left text-[12px] text-fg-dim">
          {error.message || String(error)}
        </pre>
        <button
          type="button"
          onClick={this.reload}
          className="rounded border border-line px-4 py-1.5 text-[13px] text-fg transition-colors hover:bg-ink-850"
        >
          重新加载
        </button>
      </div>
    )
  }
}
