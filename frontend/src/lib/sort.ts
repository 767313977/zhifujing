/**
 * 表头点击排序的共用逻辑。
 *
 * 全站十几张数据表都用这一套，而不是每张表各写一份「点列头 → 排序」——
 * 那种重复最容易出现「这张表 null 排最前、那张表排最后」这类不一致，
 * 而用户看到的都是「排序坏了」。
 *
 * 用法：
 *
 * ```tsx
 * const SORTS = {
 *   amount: { value: (row) => row.amount },          // 数值列，默认先看最大的
 *   name: { value: (row) => row.name, first: 'asc' } // 文字列，默认从 A 开始
 * }
 * const [sort, shown] = useSort(rows, SORTS, { key: 'amount' })
 * // 表头：<SortTh sortKey="amount" {...sort}>成交额</SortTh>
 * // 表体：shown.map(...)
 * ```
 */

import { useState } from 'react'

export type SortDir = 'asc' | 'desc'

export type SortValue = number | string | null | undefined

export interface SortSpec<T> {
  /** 取这一列的排序值。返回 null / undefined 表示这只票这一列没有数据 */
  value: (row: T) => SortValue
  /**
   * 首次点这一列时的方向。
   *
   * 默认 `desc`：表里绝大多数列是「越大越值得看」（涨幅、成交额、净买额、评分），
   * 先给最大的那一头才符合直觉。文字列（代码、名称、行业）显式传 `asc`。
   */
  first?: SortDir
}

export type SortSpecs<T> = Record<string, SortSpec<T>>

/** 空串也算缺数据 —— 后端偶尔给 `''` 而不是 `null` */
function isBlank(value: SortValue): boolean {
  return value === null || value === undefined || value === ''
}

/**
 * 按某一列排序。
 *
 * **缺数据一律排最后，不随升降序翻转。** 升序时把 null 顶到最前面，会把真正
 * 有数据的行全挤出屏幕 —— 那是「看不到」而不是「排得好」。
 *
 * 用 `Array.prototype.sort`（ES2019 起保证稳定）：值相同的行保持原顺序，
 * 所以不排的时候看到的就是后端给的顺序，排的时候同分行也不会乱跳。
 */
export function sortRows<T>(
  rows: T[],
  specs: SortSpecs<T>,
  key: string | null,
  dir: SortDir,
): T[] {
  const spec = key === null ? undefined : specs[key]
  if (!spec) return rows
  const factor = dir === 'asc' ? 1 : -1
  return [...rows].sort((a, b) => {
    const va = spec.value(a)
    const vb = spec.value(b)
    const blankA = isBlank(va)
    const blankB = isBlank(vb)
    if (blankA || blankB) {
      if (blankA && blankB) return 0
      return blankA ? 1 : -1
    }
    if (typeof va === 'number' && typeof vb === 'number') {
      return (va - vb) * factor
    }
    // 带 'zh' 才是按拼音；不给 locale 会退化成按 Unicode 码位（「阿」会排在「张」后面）
    return String(va).localeCompare(String(vb), 'zh') * factor
  })
}

export interface SortProps {
  /** 当前排序列。null = 不排，保持后端顺序 */
  activeKey: string | null
  dir: SortDir
  /** 点列头。同一列再点一次翻转方向，换一列则用该列自己的 `first` */
  onToggle: (key: string) => void
}

/**
 * 排序状态 + 排好序的行。
 *
 * 返回 `[表头属性, 排好序的行]` 两件套，表头那份**正好**是 `SortTh` 要的三个
 * prop，可以整个铺开：`<SortTh sortKey="amount" {...sort}>`。
 * 特意用元组而不是 `{ activeKey, dir, onToggle, rows }` 一个对象：
 * 后者铺开时会把 `rows` 也带进组件，而字段名叫 `key` 的话更糟 ——
 * JSX 的 `key` 是 React 保留属性，会被当成列表 key 而不是排序字段。
 *
 * `initial.key` 传 `null` 表示**首屏不排序**，直接用后端的顺序 ——
 * 后端给的往往是有意义的顺序（涨停池按首封时间、ETF 按净申赎），
 * 前端再排一遍只会把它盖掉。传了 key 的那种表（命中列表按评分）
 * 结果与后端一致，但列头能正确显示当前排的是哪一列。
 *
 * 刻意**不做「第三次点回原样」的三态循环**：在两种状态之间来回时很容易点过头，
 * 而「回到后端顺序」这个诉求并不常见。要复原就刷新页面。
 */
export function useSort<T>(
  rows: T[],
  specs: SortSpecs<T>,
  initial?: { key: string | null; dir?: SortDir },
): [SortProps, T[]] {
  const [state, setState] = useState<{ key: string | null; dir: SortDir }>(() => {
    const key = initial?.key ?? null
    if (key === null) return { key: null, dir: 'desc' }
    return { key, dir: initial?.dir ?? specs[key]?.first ?? 'desc' }
  })

  const onToggle = (key: string) => {
    setState((current) =>
      current.key === key
        ? { key, dir: current.dir === 'desc' ? 'asc' : 'desc' }
        : { key, dir: specs[key]?.first ?? 'desc' },
    )
  }

  // 不 memo：这些表最多几千行，排一次不到 1ms；而 memo 的依赖里放 specs
  // （每次渲染新建的对象）等于没 memo，反而容易写出「依赖忘了加」的 bug
  return [
    { activeKey: state.key, dir: state.dir, onToggle },
    sortRows(rows, specs, state.key, state.dir),
  ]
}
