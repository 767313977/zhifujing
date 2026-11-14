/**
 * 侧栏数据的短期缓存（S1-C6 / S2-B2）。
 *
 * 规则（全部写死在代码里，页面不需要再想一遍）：
 *
 * - 键 = `用户 ID | 股票代码 | 观察日 | 资源名`，所以**不同账户、不同股票、
 *   不同观察日不会共用一份缓存**；
 * - TTL = 3 分钟：财报 / 日线都是日频，3 分钟内重复打开同一只票不该再发请求；
 *   跨过数据日（`selectedAsOfDate` 变了）键就变了，自然不会命中旧日期；
 * - **401 / 403 不缓存**，其它失败也不缓存 —— 失败响应长期驻留会让「重试」按钮
 *   永远拿到旧错误；
 * - 缓存只存在于当前标签页内存里，刷新即清空；登出 / 换账户时由 Provider 调
 *   `clearResearchCache()` 主动清掉；
 * - **epoch（登录代次）**：`loadCached` 发起时记下当前 epoch，请求成功回来时
 *   再比一次 —— 登出 / 换账户让 epoch 递增后，旧请求的结果**不会**写回缓存，
 *   也不会被新的同 key 请求复用。`inflight` 记录同样带 epoch，旧请求的
 *   `finally` 只删自己那条，不会误删新代次的在途请求。
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

interface Inflight {
  epoch: number
  promise: Promise<unknown>
}

const cache = new Map<string, Entry>()

/**
 * 同一 key 的在途请求。侧栏标题与「概览」Tab 会同时请求同一份 profile ——
 * 没有这张表就会并发打两次。命中同 epoch 的在途 Promise 时直接复用它。
 */
const inflight = new Map<string, Inflight>()

/**
 * 登录代次。登出、切换账户、清空缓存都会 `+1`。
 * 旧代次里发出去的请求即使晚到，也不允许把数据写进新代次的缓存。
 */
let epoch = 0

export function researchCacheEpoch(): number {
  return epoch
}

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

/**
 * 登出 / 换账户时调用：不允许上一个账户的数据留在内存里。
 * 同时推进 epoch，让所有在途请求的结果作废。
 */
export function clearResearchCache(): void {
  epoch += 1
  cache.clear()
  inflight.clear()
}

/** 测试用：读当前缓存条数，不参与业务逻辑 */
export function researchCacheSize(): number {
  return cache.size
}

/** 测试用：读当前在途请求条数，不参与业务逻辑 */
export function researchInflightSize(): number {
  return inflight.size
}

/**
 * 命中则直接返回；未命中才请求。**失败一律不写缓存**；401/403 额外清掉旧值，
 * 避免权限变化后还拿到上一份成功结果。
 *
 * epoch 语义：
 * - 同 key 的在途请求只在**同一代次**内复用；
 * - 结果写缓存前比对 epoch，过期代次直接丢弃；
 * - `finally` 只在当前条目仍属于自己时删除，避免旧请求删掉新请求。
 */
export async function loadCached<T>(
  key: string,
  loader: () => Promise<T>,
  ttl = RESEARCH_CACHE_TTL,
): Promise<T> {
  const hit = readResearchCache<T>(key, ttl)
  if (hit !== undefined) return hit

  const running = inflight.get(key)
  if (running && running.epoch === epoch) return running.promise as Promise<T>

  const requestEpoch = epoch
  let self: Inflight | null = null
  const task = (async () => {
    try {
      const value = await loader()
      if (requestEpoch === epoch) writeResearchCache(key, value)
      return value
    } catch (err) {
      if (requestEpoch === epoch && err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        invalidateResearchCache(key)
      }
      throw err
    } finally {
      if (self && inflight.get(key) === self) inflight.delete(key)
    }
  })()
  self = { epoch: requestEpoch, promise: task }
  inflight.set(key, self)
  return task
}
