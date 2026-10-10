import { Link } from 'react-router-dom'
import Layout from '../components/Layout'
import Panel from '../components/Panel'

/**
 * 页面不存在（兜底路由）。
 *
 * 为什么必须有：后端对任意非 `/api` 路径都回退 `index.html`，所以手输错路径、
 * 访问缺参数的 URL（如 `/stock` 少个 code）、或点旧书签，都会把请求交给前端 ——
 * 若路由表没有 `path="*"`，React 匹配不到任何 Route 就渲染 `null`，用户看到的是
 * **全黑空页，连顶栏和回首页入口都没有**。这里按站内风格给一句说明 + 一条回首页的路。
 */
export default function NotFound() {
  return (
    <Layout>
      <Panel title="页面不存在" delay={30}>
        <div className="px-4 py-10 text-center text-[14px] text-fg-dim">
          没找到这个地址对应的页面 —— 可能是链接输错了，或这个地址需要带上参数
          （比如个股页是 <span className="num text-fg-muted">/stock/代码</span>）。
          <div className="mt-4">
            <Link to="/" className="text-accent hover:underline">
              ← 回首页
            </Link>
          </div>
        </div>
      </Panel>
    </Layout>
  )
}
