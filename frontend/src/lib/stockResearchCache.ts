/**
 * 侧栏数据的短期缓存（S1-C6）。
 *
 * 规则（全部写死在代码里，页面不需要再想一遍）：
 *
 * - 键 = `用户 ID | 股票代码 | 数据日期 | 资源名`，所以**不同账户、不同股票、
 *   不同数据日期不会共用一份缓存**；
 * - TTL = 3 分钟：财报 / 日线都是日频，3 分钟内重复打开同一只票不该再发请求；
 *   跨过数据日（`asOfDate` 变了）键就变了，自然不会命中旧日期；
 * - **401 / 403 不缓存**，其它失败也不缓存 —— 失败响应长期驻留会让「重试」按钮
 *   永远拿到旧错误；
 * - 缓存只存在于当前标签页内存里，刷新即清空；登出 / 换账户时由 Provider 调
 *   `clearResearchCache()` 主动清掉。
 */

import { ApiError } from '../api/client'

/** 默认有效期（毫秒） */
export const RESEARCH_CACHE_TTL = 3 * 60 * 1000

/** 最多保留多少条：40 只票 × 6 个 Tab ≈ 一屏反复访问的范围，再多没有意义 */
const MAX_ENTRIES = 240

interface Entry {
  at: number
  value: unknown
}

const cache = new Map<string, Entry>()

export function readResearchCache<T>(key: string, ttl = RESEARCH_CACHE_TTL): T | undefined {
  const entry = cache.get(key)
  if (!entry) return undefined
  if (Date.now() - entry.at > ttl) {
    cache.delete(key)
    return undefined
  }
  return entry.value as T
}

export function writeResearchCache(key: string, value: unknown): void {
  if (cache.size >= MAX_ENTRIES) {
    // 简单 FIFO：Map 保持插入顺序，删最早的一条
    const oldest = cache.keys().next().value
    if (oldest !== undefined) cache.delete(oldest)
  }
  cache.set(key, { at: Date.now(), value })
}

export function invalidateResearchCache(key: string): void {
  cache.delete(key)
}

/** 登出 / 换账户时调用：不允许上一个账户的数据留在内存里。 */
export function clearResearchCache(): void {
  cache.clear()
}

/** 测试用：读当前缓存条数，不参与业务逻辑 */
export function researchCacheSize(): number {
  return cache.size
}

/**
 * 命中则直接返回；未命中才请求。**失败一律不写缓存**；401/403 额外清掉旧值，
 * 避免权限变化后还拿到上一份成功结果。
 */
export async function loadCached<T>(
  key: string,
  loader: () => Promise<T>,
  ttl = RESEARCH_CACHE_TTL,
): Promise<T> {
  const hit = readResearchCache<T>(key, ttl)
  if (hit !== undefined) return hit
  try {
    const value = await loader()
    writeResearchCache(key, value)
    return value
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
      invalidateResearchCache(key)
    }
    throw err
  }
}
