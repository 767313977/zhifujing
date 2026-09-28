import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Account from './pages/Account'
import Dashboard from './pages/Dashboard'
import Funds from './pages/Funds'
import LimitReview from './pages/LimitReview'
import Login from './pages/Login'
import Patterns from './pages/Patterns'
import PatternTrack from './pages/PatternTrack'
import Sectors from './pages/Sectors'
import SentimentPage from './pages/Sentiment'
import Settings from './pages/Settings'
import StockDetail from './pages/StockDetail'
import WatchlistPage from './pages/Watchlist'
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
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}
