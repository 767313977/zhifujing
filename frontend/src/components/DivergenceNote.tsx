import type { Divergence } from '../api/types'

/**
 * 「指数涨跌 vs 个股广度」的判读条。
 *
 * 为什么单独一条而不是塞进情绪面板：这句话回答的是**今天该怎么读盘**
 * （权重拉抬还是题材活跃），和旁边那些「多少家涨停」是两类信息。
 * 放在指数条正下方，是因为它的依据就是上面那排指数。
 *
 * 三种结论一律显示（不是只在背离时才弹）：同向（普涨/普跌）本身也是判断 ——
 * 没有这条时，用户会以为页面只是没算，而不是「今天没有背离」。
 * 背后那条 `detail` 一定要带上：结论是判读口径，原始数字得摆在旁边可核对。
 */
const TONE: Record<Divergence['level'], { bar: string; text: string; label: string }> = {
  // 权重拉抬 = 要警惕的信号，用警示色
  weight_pull: { bar: 'bg-danger', text: 'text-danger', label: '警惕' },
  // 题材活跃 = 偏机会，用强调色
  theme_active: { bar: 'bg-accent', text: 'text-accent', label: '机会' },
  aligned: { bar: 'bg-fg-dim', text: 'text-fg-muted', label: '同向' },
}

export default function DivergenceNote({ data }: { data: Divergence | null }) {
  if (!data) {
    // 历史日期没有涨跌家数（乐咕只给当日），如实说明而不是显示一句假的结论
    return (
      <div className="flex items-start gap-2.5 border border-line-soft px-4 py-2.5 text-[12px] leading-relaxed text-fg-dim">
        <span className="mt-[5px] h-[6px] w-[6px] shrink-0 bg-fg-dim" />
        <span>所选日期没有涨跌家数（该数据源只提供当日值），无法判断指数与个股是否背离。</span>
      </div>
    )
  }

  const tone = TONE[data.level] ?? TONE.aligned
  return (
    <div className="flex items-start gap-2.5 border border-line-soft px-4 py-2.5 text-[12px] leading-relaxed">
      <span className={`mt-[5px] h-[6px] w-[6px] shrink-0 ${tone.bar}`} />
      <span>
        <span className={`${tone.text} mr-2 text-[12px] tracking-[0.1em]`}>
          {tone.label}
        </span>
        <span className="text-fg">{data.title}</span>
        <span className="num ml-3 text-[12px] text-fg-dim">{data.detail}</span>
      </span>
    </div>
  )
}
