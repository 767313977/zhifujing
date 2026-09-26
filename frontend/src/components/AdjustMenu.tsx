import { useEffect, useRef, useState } from 'react'
import type { FqMode } from '../api/types'
import { FQ_ITEMS, fqLabel } from '../lib/klinePeriod'

/**
 * 复权菜单 —— 照**同花顺**那个右键菜单做的（用户 2026-09-26 给的截图）。
 *
 * 与截图的三处差异，都是有意为之：
 * 1. **没有「高级复权」**：同花顺那里点开是一个设置面板（复权方式 + 除权点 + 是否考虑
 *    配股），本站没有对应的取数口径，摆一个点不动的入口不如不摆。
 * 2. **「除权(不复权)」不显示快捷键**：同花顺是 `Ctrl+C`，而浏览器里那是复制 ——
 *    抢掉之后页面上选不中文字就没法复制了。只绑 Ctrl+Q / Ctrl+B 两项。
 * 3. **「成交量复权」放在最后一行、与复权档位同一张菜单**：截图里它就是这么摆的，
 *    它是个**独立开关**（勾了不换档位），所以与上面三档之间画一条分隔线。
 *
 * 三个档位的说明写在 `klinePeriod.FQ_ITEMS` 里，与图标题栏共用一份，别在这里再写一遍。
 */
export default function AdjustMenu({
  fq,
  volAdjust,
  onChange,
  onToggleVolume,
}: {
  fq: FqMode
  /** 成交量是否跟着复权比例缩放 */
  volAdjust: boolean
  onChange: (next: FqMode) => void
  onToggleVolume: (next: boolean) => void
}) {
  const [open, setOpen] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)

  // 点外面关掉。用 mousedown 而不是 click：click 要等 mouseup，拖一下再松开会有
  // 「点了外面却还把菜单留在那」的空档
  useEffect(() => {
    if (!open) return
    const onDown = (event: MouseEvent) => {
      if (!boxRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // Ctrl+Q / Ctrl+B 切复权（同花顺的键位）。挂在 window 上、只在菜单挂载期间生效，
  // 个股页卸载就没了 —— 不给别的页面留全局副作用
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return
      const key = event.key.toLowerCase()
      if (key === 'q') {
        event.preventDefault()
        onChange('qfq')
      } else if (key === 'b') {
        event.preventDefault()
        onChange('hfq')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onChange])

  const rowClass =
    'flex w-full items-center gap-2 px-2 py-[5px] text-left text-[13px] transition-colors'

  return (
    <div className="relative" ref={boxRef}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-haspopup="menu"
        aria-expanded={open}
        title="复权方式（Ctrl+Q 向前复权 / Ctrl+B 向后复权）"
        className="flex items-center gap-1 border border-line px-2 py-[3px] text-[13px] text-fg-muted transition-colors hover:text-fg"
      >
        <span className="text-fg">{fqLabel(fq)}</span>
        <span className="text-[10px] text-fg-dim">▼</span>
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full z-20 mt-1 w-[184px] border border-line bg-ink-850 py-1 shadow-[0_8px_24px_rgba(0,0,0,0.55)]"
        >
          {FQ_ITEMS.map((item) => (
            <div key={item.key}>
              {/* 「除权(不复权)」上面画一条分隔线 —— 截图里那三档与它之间就是隔开的 */}
              {item.key === 'none' && <div className="my-1 border-t border-line-soft" />}
              <button
                type="button"
                role="menuitemradio"
                aria-checked={fq === item.key}
                title={item.hint}
                onClick={() => {
                  onChange(item.key)
                  setOpen(false)
                }}
                className={`${rowClass} ${
                  fq === item.key ? 'bg-ink-700 text-fg' : 'text-fg-muted hover:bg-ink-800'
                }`}
              >
                {/* 勾选标记占位固定宽度：不占位的话选中项的文字会往左跳一格 */}
                <span className={`w-3 text-[12px] ${fq === item.key ? 'text-accent' : ''}`}>
                  {fq === item.key ? '✓' : ''}
                </span>
                <span>{item.label}</span>
                {item.shortcut && (
                  <span className="num ml-auto text-[12px] text-fg-dim">{item.shortcut}</span>
                )}
              </button>
            </div>
          ))}
          <div className="my-1 border-t border-line-soft" />
          {/* 独立开关：不复权时比例恒为 1，勾了也不生效，所以在 hint 里说明 */}
          <button
            type="button"
            role="menuitemcheckbox"
            aria-checked={volAdjust}
            title="成交量按同一复权比例缩放，让除权日前后的量能可比；不复权时比例恒为 1，勾了等于没勾"
            onClick={() => {
              onToggleVolume(!volAdjust)
              setOpen(false)
            }}
            className={`${rowClass} ${volAdjust ? 'text-fg' : 'text-fg-muted'} hover:bg-ink-800`}
          >
            <span className={`w-3 text-[12px] ${volAdjust ? 'text-accent' : ''}`}>
              {volAdjust ? '✓' : ''}
            </span>
            <span>成交量复权</span>
          </button>
        </div>
      )}
    </div>
  )
}
