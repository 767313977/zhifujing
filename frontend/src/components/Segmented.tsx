/**
 * 段选控件（一排按钮里选一个）。
 *
 * 抽成公共组件是因为站内已经有三处要用同一套手感（板块页的精选/行业、轮动矩阵的
 * 指标与周期、个股页的 K 线周期）：各自写一份的话，改样式或改选中态时总会漏掉一处，
 * 用户看到的就是「同一个控件在两个页面长得不一样」。
 *
 * 可访问性（2026-10-23）：选中态以前只靠颜色，读屏完全看不出当前选了哪一档。
 * 现在按 radiogroup/radio 语义标注，`aria-checked` 跟着 `value` 走；
 * 键盘交互仍是原生 tab + Enter/Space（按钮自带），不自己造 roving tabindex。
 */
export default function Segmented<T extends string | number>({
  value,
  items,
  onChange,
}: {
  value: T
  items: { key: T; label: string; hint?: string }[]
  onChange: (next: T) => void
}) {
  return (
    <div role="radiogroup" className="flex items-stretch border border-line">
      {items.map((item) => {
        const selected = value === item.key
        return (
          <button
            key={String(item.key)}
            type="button"
            role="radio"
            aria-checked={selected}
            title={item.hint}
            onClick={() => onChange(item.key)}
            className={[
              'px-2 py-[3px] text-[13px] transition-colors',
              selected ? 'bg-ink-700 text-fg' : 'text-fg-muted hover:text-fg',
            ].join(' ')}
          >
            {item.label}
          </button>
        )
      })}
    </div>
  )
}
