/**
 * S6.7：根级 ErrorBoundary 必须有可见 fallback（2026-11-15）。
 *
 * 白屏缺陷的教训：任何未预期的渲染异常都不能让 `#root` 变成空树。
 * 这里直接渲染一个必抛组件，断言用户能看到「页面加载失败」和错误信息。
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import ErrorBoundary from '../ErrorBoundary'

function Bomb(): never {
  throw new Error('boom-from-test')
}

describe('S6.7 ErrorBoundary 可见 fallback', () => {
  it('渲染期异常显示错误页而不是空白', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    )
    expect(screen.getByText('页面加载失败')).toBeTruthy()
    expect(screen.getByText('boom-from-test')).toBeTruthy()
    expect(screen.getByRole('button', { name: '重新加载' })).toBeTruthy()
    spy.mockRestore()
  })
})
