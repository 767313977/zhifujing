/**
 * S6.8 E15：Chunk 加载失败的自愈必须**限流**（2026-11-15）。
 *
 * 旧实现每次启动清标记，持久 404 会无限刷新；这里锁住新规则：
 * 10 秒内只自动重载一次，第二次交给 ErrorBoundary。
 */

import { describe, expect, it } from 'vitest'

import {
  CHUNK_RELOAD_KEY,
  CHUNK_RELOAD_WINDOW_MS,
  decideChunkReload,
} from '../chunkReload'

function memoryStorage(initial: Record<string, string> = {}) {
  const data = { ...initial }
  return {
    data,
    getItem: (key: string) => (key in data ? data[key] : null),
    setItem: (key: string, value: string) => {
      data[key] = value
    },
  }
}

describe('S6.8 decideChunkReload', () => {
  it('首次失败允许自动重载一次', () => {
    const storage = memoryStorage()
    expect(decideChunkReload(storage, 1_000)).toBe('reload')
    expect(storage.data[CHUNK_RELOAD_KEY]).toBe('1000')
  })

  it('窗口内再次失败不重载，交给 ErrorBoundary', () => {
    const storage = memoryStorage()
    expect(decideChunkReload(storage, 1_000)).toBe('reload')
    expect(decideChunkReload(storage, 1_000 + CHUNK_RELOAD_WINDOW_MS - 1)).toBe(
      'fallback',
    )
  })

  it('超过窗口后恢复一次自愈机会', () => {
    const storage = memoryStorage()
    decideChunkReload(storage, 1_000)
    expect(decideChunkReload(storage, 1_000 + CHUNK_RELOAD_WINDOW_MS)).toBe('reload')
  })

  it('存储不可用时 fail safe，不制造刷新循环', () => {
    expect(decideChunkReload(null, 1_000)).toBe('fallback')
    const broken = {
      getItem: () => {
        throw new Error('SecurityError')
      },
      setItem: () => {
        throw new Error('SecurityError')
      },
    }
    expect(decideChunkReload(broken, 1_000)).toBe('fallback')
  })
})
