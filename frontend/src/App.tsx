import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import LimitReview from './pages/LimitReview'
import Placeholder from './pages/Placeholder'
import Screener from './pages/Screener'
import SentimentPage from './pages/Sentiment'
import Settings from './pages/Settings'
import StockDetail from './pages/StockDetail'
import WatchlistPage from './pages/Watchlist'

/** 已规划但尚未实现的页面，先占住路由，导航不会失效。 */
const UPCOMING = [
  {
    path: '/sectors',
    title: '板块题材',
    desc: '板块排行、板块内个股、板块历史强度',
  },
]

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/sentiment" element={<SentimentPage />} />
        <Route path="/limit-up" element={<LimitReview />} />
        <Route path="/screener" element={<Screener />} />
        <Route path="/watchlist" element={<WatchlistPage />} />
        <Route path="/stock/:code" element={<StockDetail />} />
        <Route path="/settings" element={<Settings />} />
        {UPCOMING.map((item) => (
          <Route
            key={item.path}
            path={item.path}
            element={<Placeholder title={item.title} desc={item.desc} />}
          />
        ))}
      </Routes>
    </BrowserRouter>
  )
}
