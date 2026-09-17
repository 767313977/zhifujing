import Layout from '../components/Layout'

interface PlaceholderProps {
  title: string
  desc: string
}

/** 尚未实现的页面。保留路由让导航可用，也标明后续范围。 */
export default function Placeholder({ title, desc }: PlaceholderProps) {
  return (
    <Layout>
      <div className="panel rise flex h-64 flex-col items-center justify-center gap-2.5">
        <span className="text-[15px] tracking-[0.2em] text-fg-muted">{title}</span>
        <span className="max-w-md text-center text-[12px] leading-relaxed text-fg-dim">
          {desc}
        </span>
        <span className="num mt-1.5 border border-line px-2 py-0.5 text-[10px] tracking-wider text-fg-dim">
          待开发
        </span>
      </div>
    </Layout>
  )
}
