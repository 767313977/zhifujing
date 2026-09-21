import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import Funds from './pages/Funds'
import LimitReview from './pages/LimitReview'
import Patterns from './pages/Patterns'
import Screener from './pages/Screener'
import Sectors from './pages/Sectors'
import SentimentPage from './pages/Sentiment'
import Settings from './pages/Settings'
import StockDetail from './pages/StockDetail'
import WatchlistPage from './pages/Watchlist'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/sentiment" element={<SentimentPage />} />
        <Route path="/sectors" element={<Sectors />} />
        <Route path="/limit-up" element={<LimitReview />} />
        <Route path="/funds" element={<Funds />} />
        <Route path="/patterns" element={<Patterns />} />
        <Route path="/screener" element={<Screener />} />
        <Route path="/watchlist" element={<WatchlistPage />} />
        <Route path="/stock/:code" element={<StockDetail />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>
    </BrowserRouter>
  )
}
