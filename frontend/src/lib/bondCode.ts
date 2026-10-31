/**
 * 可转债代码识别（2026-10-30 可转债-5）。
 *
 * 自选里允许放转债代码，但**转债没有个股详情页** —— 各处渲染「代码 / 名称」时
 * 用这个判一下：转债走 `/cb/:code` 单券详情，股票才走 `/stock/:code`，
 * 免得点进去看到「代码不存在」。
 *
 * 前缀：沪市 110 / 111 / 113 / 118 / 132，深市 123 / 127 / 128。
 * 这几个前缀与股票（600/601/603/605/688、000/001/002/003/300/301）不重叠。
 */
const BOND_PREFIX = /^(110|111|113|118|132|123|127|128)/

export function isBondCode(code: string): boolean {
  return BOND_PREFIX.test(code)
}

/**
 * ETF / LOF 代码前缀（2026-10-31 ETF-5）。与后端 `services/watch_scope.ETF_PREFIXES`
 * 同一套：沪 50/51/52/56/58（588 是科创 ETF）、深 15/16/18。
 *
 * ETF 同样**没有个股详情页**（ETF 只有筛选页，没有单只页面），所以渲染时也要
 * 走 `hasStockPage`，别把 510300 做成 `/stock/510300` 的链接。
 */
const ETF_PREFIX = /^(15|16|18|50|51|52|56|58)/

export function isEtfCode(code: string): boolean {
  return ETF_PREFIX.test(code)
}

/** 有 `/stock/:code` 个股页的才返回 true（转债走 `/cb/:code`，ETF 没有详情页）。 */
export function hasStockPage(code: string): boolean {
  return !isBondCode(code) && !isEtfCode(code)
}
