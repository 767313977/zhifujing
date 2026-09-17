import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import Placeholder from './pages/Placeholder'
import SentimentPage from './pages/Sentiment'

/** 已规划但尚未实现的页面，先占住路由，导航不会失效。 */
const UPCOMING = [
  {
    path: '/sectors',
    title: '板块题材',
    desc: '板块排行、板块内个股、板块历史强度',
  },
  {
    path: '/limit-up',
    title: '涨停复盘',
    desc: '按连板高度分层，标注所属题材',
  },
  {
    path: '/screener',
    title: '选股器',
    desc: 'iFinD 自然语言选股（主力）+ 本地结构化条件筛选',
  },
  {
    path: '/watchlist',
    title: '自选股',
    desc: '自选池、每日表现、复盘笔记',
  },
  {
    path: '/settings',
    title: '数据管理',
    desc: '采集状态、手动补数、采集日志',
  },
]

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/sentiment" element={<SentimentPage />} />
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
