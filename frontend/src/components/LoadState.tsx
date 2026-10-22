/**
 * 面板 / 表格的「加载中 / 加载失败 / 暂无数据」三态（2026-10-22 体检）。
 *
 * 为什么值得单独一个组件：以前这些地方一律显示「暂无数据」，而接口 500 / 超时
 * 也会把数据置空 —— 于是「今天真的没有」和「接口挂了」长得一模一样，排查时
 * 完全看不出是后端出错。失败时这里会明确写「加载失败」，并把原因带出来。
 *
 * 注意：**404 不算失败**。那几个增强读数（恐贪 / 宽度 / ERP…）在库里还没有
 * 数据时后端就是返回 404，那是正常的「暂无数据」，调用方要把 404 过滤掉
 * （见 `pages/Sentiment.tsx` 的 `track`）。
 */
interface LoadStateProps {
  /** 请求失败原因；空值表示「正常但没有数据」 */
  error?: string | null
  /** 取数中：优先于空态显示「加载中…」 */
  loading?: boolean
  /** 正常空态的文案 */
  emptyText?: string
  /** 外层留白，缺省与各面板一致（`px-4 py-8`） */
  className?: string
}

export default function LoadState({
  error,
  loading = false,
  emptyText = '暂无数据',
  className = 'px-4 py-8',
}: LoadStateProps) {
  if (loading) {
    return (
      <div className={`${className} text-center text-[14px] text-fg-dim`}>
        <span className="pulse-soft">加载中…</span>
      </div>
    )
  }
  if (error) {
    return (
      <div className={`${className} text-center text-[14px] text-fg-dim`}>
        <span className="text-accent">加载失败</span>：{error}
        <div className="mt-1 text-[12px]">这是接口出错，不是「没有数据」，稍后刷新重试</div>
      </div>
    )
  }
  return (
    <div className={`${className} text-center text-[14px] text-fg-dim`}>{emptyText}</div>
  )
}
