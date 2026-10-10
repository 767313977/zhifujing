import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Account from './pages/Account'
import AnomalyPage from './pages/Anomaly'
import Dashboard from './pages/Dashboard'
import Funds from './pages/Funds'
import LimitReview from './pages/LimitReview'
import Login from './pages/Login'
import NotFound from './pages/NotFound'
import Patterns from './pages/Patterns'
import PatternTrack from './pages/PatternTrack'
import Sectors from './pages/Sectors'
import SentimentPage from './pages/Sentiment'
import Settings from './pages/Settings'
import StockAnalysisPage from './pages/StockAnalysis'
import StockDetail from './pages/StockDetail'
import WatchlistPage from './pages/Watchlist'
import Wudao from './pages/Wudao'
import { AuthProvider, RequireAdmin, RequireAuth } from './lib/auth'

/**
 * 路由。**除了 `/login`，所有页面都要登录**（设计见文档 §8.69）。
 *
 * 用 `RequireAuth` 逐个包住路由，而不是在 Layout 里判断：不是每个页面都渲染
 * Layout（登录页就不渲染），把关卡放在 Layout 里会漏掉那条路。
 *
 * `AuthProvider` 必须在 `BrowserRouter` 内（它要用 useNavigate）。
 */
export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
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
          <Route
            path="/sectors"
            element={
              <RequireAuth>
                <Sectors />
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
            path="/watchlist"
            element={
              <RequireAuth>
                <WatchlistPage />
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
      </AuthProvider>
    </BrowserRouter>
  )
}
