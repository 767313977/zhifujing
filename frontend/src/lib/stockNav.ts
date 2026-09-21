/**
 * 个股详情页的「上一只 / 下一只」上下文。
 *
 * 各列表页在跳转前把**当前展示的那份列表**存进 sessionStorage，详情页读出来
 * 就能用 ← → 前后翻。
 *
 * 为什么不把列表放进 URL：跳转本身走的是 `<Link>`，同一个点击里如果再动一次
 * router（比如 `setSearchParams`）会连发两次导航 —— Patterns 页为此踩过坑，
 * 表现为「点一下只选中、要点两下才跳转」。sessionStorage 是同步写入、完全不碰
 * router，不会干扰那一次导航。
 *
 * 存成 sessionStorage 而不是 localStorage：它是「这次浏览的上下文」，
 * 隔天打开旧标签页时不该还惦记着昨天那份列表。
 */

const KEY = 'stock-nav:codes'

/** 本次会话里已经补过历史日线的代码 */
const SYNCED_KEY = 'stock-nav:synced'

/** 这只票补过 2 年历史（给周/月 K 用）。存 localStorage，见 `markLongHistory` */
const LONG_KEY = 'stock-nav:synced-long'

/**
 * 通用读法：拿不到、或内容坏掉时一律当空列表，绝不抛错往上冒。
 * `store` 与 `mark()` 的取值对应 —— 标记存在哪就要从哪读，
 * 只改一边会变成「每次都补一次」（`localStorage` 与 `sessionStorage` 不互通）。
 */
function readCodes(key: string, store: 'session' | 'local' = 'session'): string[] {
  try {
    const raw = storage(store).getItem(key)
    const parsed: unknown = raw ? JSON.parse(raw) : null
    if (!Array.isArray(parsed)) return []
    return parsed.filter((item): item is string => typeof item === 'string')
  } catch {
    return []
  }
}

/** 记住这份列表。各列表页在点击个股链接时调用 */
export function rememberStockList(codes: string[]): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(codes))
  } catch {
    // 隐私模式下 sessionStorage 可能直接抛错。退化成「不支持前后翻」而不是白屏
  }
}

/** 读回上次记住的列表。拿不到、或内容坏掉时返回空数组 */
export function readStockList(): string[] {
  return readCodes(KEY)
}

/**
 * 标记这只票本次会话已经补过历史日线。
 *
 * 用来兜住「历史本来就短」的票（次新股）：它们的 `day_count` 永远到不了阈值，
 * 只靠 `day_count` 判断的话每次打开都会再补一遍，一只票六次调用。
 * 隔一次会话允许再补，是给「新上市、又多了一天数据」留的余地。
 *
 * ⚠️ **只在补采成功之后调**。提前标会让失败的那次也算数，本会话内再打开
 * 这只票就不再重试 —— 表现是「点进去没日K、刷新也没用」。
 */
export function markSynced(code: string): void {
  mark(SYNCED_KEY, code)
}

export function alreadySynced(code: string): boolean {
  return readCodes(SYNCED_KEY).includes(code)
}

/**
 * 标记这只票已经补过 2 年历史（切周/月 K 时用，见 `useKLine`）。
 *
 * **用 localStorage 而不是 sessionStorage**（这里跟上面那些标记不一样）：
 * 长历史补的是**更老**的数据，对某只票只可能补到一次 —— 要么补满了 2 年，
 * 要么这只票上市就不满 2 年（次新股，再补也没有更早的了）。所以这个标记是
 * 「缓存有效性」而不是「本次浏览的视图状态」，跨会话留着才不会每次开新会话
 * 都白跑一次性 11 次 iFinD 调用。
 *
 * 只在**补采成功之后**调（失败要允许下次再试）。
 */
export function markLongHistory(code: string): void {
  mark(LONG_KEY, code, 'local')
}

export function hasLongHistory(code: string): boolean {
  return readCodes(LONG_KEY, 'local').includes(code)
}

function mark(key: string, code: string, store: 'session' | 'local' = 'session'): void {
  try {
    const done = new Set(readCodes(key, store))
    done.add(code)
    storage(store).setItem(key, JSON.stringify([...done]))
  } catch {
    // 存不进去就退化成「每次都补一次」—— 浪费几次调用，但功能是对的
  }
}

function storage(store: 'session' | 'local'): Storage {
  return store === 'local' ? localStorage : sessionStorage
}
