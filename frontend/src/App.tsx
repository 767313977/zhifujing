import { Suspense, lazy } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AuthProvider, RequireAdmin, RequireAuth } from './lib/auth'
import { WatchlistProvider } from './lib/watchlist'

/**
 * 页面**按需加载**（2026-10-12）。
 *
 * 以前 16 个页面全是静态 import，整站（含 ECharts）被打进**单个约 1.07 MB** 的
 * `index-*.js`，首屏要把它整包下完才开始渲染。改成 `lazy` 之后，每个页面各自成块：
 * 首屏只加载当前路由（外加很小的公共壳），其余页面在点进去时才拉。
 *
 * `AuthProvider` / `RequireAuth` 保持**静态导入** —— 它们要在第一次渲染就参与
 * 鉴权判定，不能异步。
 */
const Account = lazy(() => import('./pages/Account'))
const AnomalyPage = lazy(() => import('./pages/Anomaly'))
const AuctionPage = lazy(() => import('./pages/Auction'))
const ConvertiblePage = lazy(() => import('./pages/Convertible'))
const CbDetailPage = lazy(() => import('./pages/CbDetail'))
const EtfPage = lazy(() => import('./pages/Etf'))
const Dashboard = lazy(() => import('./pages/Dashboard'))
const Funds = lazy(() => import('./pages/Funds'))
const JournalPage = lazy(() => import('./pages/Journal'))
const LimitReview = lazy(() => import('./pages/LimitReview'))
const Login = lazy(() => import('./pages/Login'))
const NotFound = lazy(() => import('./pages/NotFound'))
const Patterns = lazy(() => import('./pages/Patterns'))
const PatternTrack = lazy(() => import('./pages/PatternTrack'))
const PlansPage = lazy(() => import('./pages/Plans'))
const PremarketPage = lazy(() => import('./pages/Premarket'))
const PoolsPage = lazy(() => import('./pages/Pools'))
const Sectors = lazy(() => import('./pages/Sectors'))
const SentimentPage = lazy(() => import('./pages/Sentiment'))
const Settings = lazy(() => import('./pages/Settings'))
const StockAnalysisPage = lazy(() => import('./pages/StockAnalysis'))
const StockDetail = lazy(() => import('./pages/StockDetail'))
const ValuationPage = lazy(() => import('./pages/Valuation'))
const WatchlistPage = lazy(() => import('./pages/Watchlist'))
const Wudao = lazy(() => import('./pages/Wudao'))

/** 页面代码块还没下完时的占位（切页很快，通常一闪而过） */
function PageFallback() {
  return (
    <div className="flex min-h-[40vh] items-center justify-center text-[14px] text-fg-dim">
      加载中…
    </div>
  )
}

/**
 * 路由。**除了 `/login`，所有页面都要登录**（设计见文档 §8.69）。
 *
 * 用 `RequireAuth` 逐个包住路由，而不是在 Layout 里判断：不是每个页面都渲染
 * Layout（登录页就不渲染），把关卡放在 Layout 里会漏掉那条路。
 *
 * `AuthProvider` 必须在 `BrowserRouter` 内（它要用 useNavigate）。
 *
 * `WatchlistProvider` 放在 `AuthProvider` 里、`Suspense` 外：它要读登录态，
 * 也要让**所有**页面（含登录页）都能拿到 context，但未登录时不会去请求名单。
 */
export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <WatchlistProvider>
          <Suspense fallback={<PageFallback />}>
            <Routes>
              <Route path="/login" element={<Login />} />
              <Route
                path="/"
                element={
                  <RequireAuth>
                    <Dashboard />
                  </RequireAuth>
                }
              />
              <Route
                path="/sentiment"
                element={
                  <RequireAuth>
                    <SentimentPage />
                  </RequireAuth>
                }
              />
              {/* 盘前 / 开盘（FEAT-PREMARKET）：昨日涨停今开 + 悟道名单 + 自选，纯读库 */}
              <Route
                path="/premarket"
                element={
                  <RequireAuth>
                    <PremarketPage />
                  </RequireAuth>
                }
              />
              <Route
                path="/valuation"
                element={
                  <RequireAuth>
                    <ValuationPage />
                  </RequireAuth>
                }
              />
              <Route
                path="/sectors"
                element={
                  <RequireAuth>
                    <Sectors />
                  </RequireAuth>
                }
              />
              <Route
                path="/auction"
                element={
                  <RequireAuth>
                    <AuctionPage />
                  </RequireAuth>
                }
              />
              <Route
                path="/limit-up"
                element={
                  <RequireAuth>
                    <LimitReview />
                  </RequireAuth>
                }
              />
              <Route
                path="/funds"
                element={
                  <RequireAuth>
                    <Funds />
                  </RequireAuth>
                }
              />
              <Route
                path="/patterns"
                element={
                  <RequireAuth>
                    <Patterns />
                  </RequireAuth>
                }
              />
              <Route
                path="/patterns/track"
                element={
                  <RequireAuth>
                    <PatternTrack />
                  </RequireAuth>
                }
              />
              <Route
                path="/pools"
                element={
                  <RequireAuth>
                    <PoolsPage />
                  </RequireAuth>
                }
              />
              {/* 可转债：盘后筛选 + 强赎雷达（数据在收盘后 17:30 那趟采） */}
              <Route
                path="/cb"
                element={
                  <RequireAuth>
                    <ConvertiblePage />
                  </RequireAuth>
                }
              />
              {/* 单券详情：条款 + 最近快照 + 走势（盘后口径，见 pages/CbDetail） */}
              <Route
                path="/cb/:code"
                element={
                  <RequireAuth>
                    <CbDetailPage />
                  </RequireAuth>
                }
              />
              {/* ETF：盘后筛选 + 行业强弱（日线来自通达信盘后包，可回补一年） */}
              <Route
                path="/etf"
                element={
                  <RequireAuth>
                    <EtfPage />
                  </RequireAuth>
                }
              />
              <Route
                path="/watchlist"
                element={
                  <RequireAuth>
                    <WatchlistPage />
                  </RequireAuth>
                }
              />
              <Route
                path="/journal"
                element={
                  <RequireAuth>
                    <JournalPage />
                  </RequireAuth>
                }
              />
              <Route
                path="/plans"
                element={
                  <RequireAuth>
                    <PlansPage />
                  </RequireAuth>
                }
              />
              <Route
                path="/stock/:code"
                element={
                  <RequireAuth>
                    <StockDetail />
                  </RequireAuth>
                }
              />
              {/* 悟道之路：移植过来的选股池（判定逻辑在 services/patterns.py） */}
              <Route
                path="/wudao"
                element={
                  <RequireAuth>
                    <Wudao />
                  </RequireAuth>
                }
              />
              {/* 个股异动：复刻原型的「豆包异动图谱」，数据全来自本站库 */}
              <Route
                path="/anomaly"
                element={
                  <RequireAuth>
                    <AnomalyPage />
                  </RequireAuth>
                }
              />
              {/* 个股分析：输代码/名称/首字母出结论（移植自原型的「查票分析」） */}
              <Route
                path="/stock-analysis"
                element={
                  <RequireAuth>
                    <StockAnalysisPage />
                  </RequireAuth>
                }
              />
              <Route
                path="/settings"
                element={
                  <RequireAdmin>
                    <Settings />
                  </RequireAdmin>
                }
              />
              <Route
                path="/account"
                element={
                  <RequireAuth>
                    <Account />
                  </RequireAuth>
                }
              />
              {/* 兜底路由：后端对任意非 /api 路径都回退 index.html，没有这条时
                  未知 URL 会渲染 null（全黑空页）。与其它页面一样套 RequireAuth ——
                  未登录用户先去登录，登录后才看得到这个提示（别绕过鉴权）。 */}
              <Route
                path="*"
                element={
                  <RequireAuth>
                    <NotFound />
                  </RequireAuth>
                }
              />
            </Routes>
          </Suspense>
        </WatchlistProvider>
      </AuthProvider>
    </BrowserRouter>
  )
}
