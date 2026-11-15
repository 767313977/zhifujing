/**
 * S6.8 A 包：真实浏览器 E2E（E13 / E15 / E16 / 三视口）。
 *
 * 为什么自建这个脚本：仓库里没有 Playwright/Cypress/Selenium（已核对
 * package.json 与 .venv），但任务书明确要求「不得因工具能力不足直接 NOT_RUN」。
 * 这里用 Node 24 自带的 WebSocket + Chrome DevTools Protocol 驱动**真实 Chrome**，
 * 不引入第二套框架、不改 package.json / lockfile。
 *
 * 覆盖：
 * - E13：Target.createBrowserContext 建两个真正独立的 Cookie 罐；
 * - E15：Fetch 域拦截真实 lazy chunk 并返回失败，验证限流自愈 + ErrorBoundary；
 * - E16：DEV-only fixture 路由触发渲染期异常，验证根级 fallback；
 * - 三视口：Emulation.setDeviceMetricsOverride + 登录/首页/导航/退出/错误 fallback。
 *
 * 用法：
 *   node scripts/s6_8_cdp_e2e.mjs \
 *     --base http://127.0.0.1:8021 \
 *     --dev-base http://127.0.0.1:8030 \
 *     --dist frontend/dist \
 *     --out reports/s6_8_9/browser
 *
 * 安全：只记录状态码、DOM 读数、页面路径与截图；不记录 Cookie/Token/密码。
 */

import { spawn } from 'node:child_process'
import { mkdirSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

const CHROME =
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const CDP_PORT = 9333
const CDP_URL = `http://127.0.0.1:${CDP_PORT}`
const USER_A = { username: 's7staging', password: 'staging-password-123' }
const USER_B = { username: 's7userb', password: 'staging-password-123' }
const sleep = (ms) => new Promise((resolveSleep) => setTimeout(resolveSleep, ms))

function parseArgs() {
  const args = process.argv.slice(2)
  const out = {}
  for (let index = 0; index < args.length; index += 2) {
    out[args[index].replace(/^--/, '')] = args[index + 1]
  }
  return {
    base: out.base ?? 'http://127.0.0.1:8021',
    devBase: out['dev-base'] ?? 'http://127.0.0.1:8030',
    dist: resolve(out.dist ?? 'frontend/dist'),
    out: resolve(out.out ?? 'reports/s6_8_9/browser'),
    // 只跑某个场景（S6.10 复用：`--only history` 只需要 --base，不依赖 dev-base/dist）
    only: out.only ?? '',
  }
}

class CDP {
  constructor(wsUrl) {
    this.wsUrl = wsUrl
    this.ws = null
    this.nextId = 1
    this.pending = new Map()
    this.listeners = new Map()
  }

  async connect() {
    this.ws = new WebSocket(this.wsUrl)
    await new Promise((resolveOpen, rejectOpen) => {
      this.ws.addEventListener('open', resolveOpen, { once: true })
      this.ws.addEventListener('error', rejectOpen, { once: true })
    })
    this.ws.addEventListener('message', (event) => {
      const message = JSON.parse(event.data)
      if (message.id !== undefined) {
        const task = this.pending.get(message.id)
        if (!task) return
        this.pending.delete(message.id)
        if (message.error) task.reject(new Error(`${message.error.message} (${message.error.code})`))
        else task.resolve(message.result)
        return
      }
      const key = message.method
      for (const handler of this.listeners.get(key) ?? []) handler(message)
    })
  }

  send(method, params = {}, sessionId) {
    const id = this.nextId++
    const payload = { id, method, params }
    if (sessionId) payload.sessionId = sessionId
    return new Promise((resolveSend, rejectSend) => {
      this.pending.set(id, { resolve: resolveSend, reject: rejectSend })
      this.ws.send(JSON.stringify(payload))
    })
  }

  on(method, handler) {
    if (!this.listeners.has(method)) this.listeners.set(method, [])
    this.listeners.get(method).push(handler)
  }

  close() {
    try {
      this.ws?.close()
    } catch {
      // ignore
    }
  }
}

async function waitForJson(url, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url)
      if (response.ok) return await response.json()
    } catch {
      // chrome 还没起来
    }
    await sleep(200)
  }
  throw new Error(`等待 Chrome CDP 超时：${url}`)
}

async function openPage(cdp, browserContextId = null) {
  const target = await cdp.send('Target.createTarget', {
    url: 'about:blank',
    browserContextId: browserContextId ?? undefined,
  })
  const attached = await cdp.send('Target.attachToTarget', {
    targetId: target.targetId,
    flatten: true,
  })
  const sessionId = attached.sessionId
  const errors = []
  cdp.on('Runtime.exceptionThrown', (message) => {
    if (message.sessionId !== sessionId) return
    errors.push({
      kind: 'exception',
      text: message.params.exceptionDetails?.exception?.description?.slice(0, 300) ?? 'exception',
    })
  })
  cdp.on('Runtime.consoleAPICalled', (message) => {
    if (message.sessionId !== sessionId || message.params.type !== 'error') return
    errors.push({
      kind: 'console.error',
      text: (message.params.args ?? [])
        .map((arg) => String(arg.value ?? arg.description ?? ''))
        .join(' ')
        .slice(0, 300),
    })
  })
  await cdp.send('Page.enable', {}, sessionId)
  await cdp.send('Runtime.enable', {}, sessionId)
  await cdp.send('Log.enable', {}, sessionId)
  await cdp.send('Network.enable', {}, sessionId)
  return { targetId: target.targetId, sessionId, errors }
}

async function evaluate(cdp, sessionId, expression) {
  const result = await cdp.send(
    'Runtime.evaluate',
    { expression, awaitPromise: true, returnByValue: true, userGesture: true },
    sessionId,
  )
  if (result.exceptionDetails) {
    throw new Error(
      `页面脚本异常：${result.exceptionDetails.exception?.description ?? result.exceptionDetails.text}`,
    )
  }
  return result.result?.value
}

async function waitUntil(cdp, sessionId, expression, timeoutMs = 15000, label = 'condition') {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      if (await evaluate(cdp, sessionId, expression)) return true
    } catch {
      // 页面中途跳转时 evaluate 可能短暂失败
    }
    await sleep(150)
  }
  throw new Error(`等待超时：${label}`)
}

async function goto(cdp, sessionId, url) {
  await cdp.send('Page.navigate', { url }, sessionId)
  await waitUntil(
    cdp,
    sessionId,
    `document.readyState === 'complete' && location.href.startsWith(${JSON.stringify(url.split('?')[0])})`,
    20000,
    `导航 ${url}`,
  )
  await sleep(300)
}

const STATE_JS = `(() => {
  const root = document.querySelector('#root')
  const text = document.body.innerText || ''
  const links = Array.from(document.querySelectorAll('a'))
  return {
    href: location.href,
    path: location.pathname + location.search,
    rootChildren: root ? root.childElementCount : -1,
    rootHtmlLength: root ? root.innerHTML.length : -1,
    bodyLen: text.length,
    textHead: text.slice(0, 160),
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    hasLoginForm: !!document.querySelector('#username'),
    visibleLinks: links.filter((a) => a.offsetParent !== null).length,
    hasMenuButton: Array.from(document.querySelectorAll('button')).some(
      (b) => (b.textContent || '').includes('打开导航菜单'),
    ),
    hasExitButton: Array.from(document.querySelectorAll('button')).some(
      (b) => (b.textContent || '').trim() === '退出',
    ),
    hasFallback: text.includes('页面加载失败'),
    hasExpiredNotice: text.includes('登录已过期'),
  }
})()`

async function pageState(cdp, sessionId) {
  return evaluate(cdp, sessionId, STATE_JS)
}

async function apiJson(cdp, sessionId, path, init = {}) {
  const expression = `fetch(${JSON.stringify(path)}, ${JSON.stringify(init)})
    .then(async (r) => ({ status: r.status, body: await r.text() }))
    .then((x) => { try { return { status: x.status, json: JSON.parse(x.body) } } catch { return { status: x.status, text: x.body.slice(0, 200) } } })`
  return evaluate(cdp, sessionId, expression)
}

async function apiLogin(cdp, sessionId, user) {
  const result = await apiJson(cdp, sessionId, '/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(user),
  })
  if (result.status !== 200) throw new Error(`登录失败：${result.status}`)
  return result.json
}

async function uiLogin(cdp, sessionId, base, user) {
  await goto(cdp, sessionId, `${base}/login`)
  await waitUntil(cdp, sessionId, `!!document.querySelector('#username')`, 15000, '登录表单')
  await evaluate(
    cdp,
    sessionId,
    `(() => {
      const set = (el, value) => {
        const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')
        descriptor.set.call(el, value)
        el.dispatchEvent(new Event('input', { bubbles: true }))
      }
      set(document.querySelector('#username'), ${JSON.stringify(user.username)})
      set(document.querySelector('#password'), ${JSON.stringify(user.password)})
      document.querySelector('button[type="submit"]').click()
      return true
    })()`,
  )
  await waitUntil(
    cdp,
    sessionId,
    `location.pathname === '/' && document.querySelector('#root').childElementCount > 0 && document.body.innerText.length > 200`,
    15000,
    '登录后进入首页',
  )
}

async function screenshot(cdp, sessionId, outDir, name) {
  // 不覆盖视口：截图必须反映调用方设置的 Emulation 尺寸（三视口证据）
  const shot = await cdp.send('Page.captureScreenshot', { format: 'png' }, sessionId)
  writeFileSync(join(outDir, 'screenshots', `${name}.png`), Buffer.from(shot.data, 'base64'))
}

function overflow(state) {
  return state.scrollWidth > state.innerWidth + 1
}

async function closeContext(cdp, browserContextId) {
  try {
    await cdp.send('Target.disposeBrowserContext', { browserContextId })
  } catch {
    // ignore
  }
}

async function runE13(cdp, base, outDir) {
  const browserContextA = (await cdp.send('Target.createBrowserContext')).browserContextId
  const browserContextB = (await cdp.send('Target.createBrowserContext')).browserContextId
  const pageA = await openPage(cdp, browserContextA)
  const pageB = await openPage(cdp, browserContextB)
  const result = { scenario: 'E13 双 Browser Context', contexts: {} }

  try {
    await goto(cdp, pageA.sessionId, `${base}/login`)
    const meA = await apiLogin(cdp, pageA.sessionId, USER_A)
    await goto(cdp, pageA.sessionId, `${base}/`)
    const stateA = await pageState(cdp, pageA.sessionId)

    await goto(cdp, pageB.sessionId, `${base}/login`)
    const meB = await apiLogin(cdp, pageB.sessionId, USER_B)
    await goto(cdp, pageB.sessionId, `${base}/`)
    const stateB = await pageState(cdp, pageB.sessionId)

    const meAAfter = await apiJson(cdp, pageA.sessionId, '/api/auth/me')
    const meBAfter = await apiJson(cdp, pageB.sessionId, '/api/auth/me')

    // 并行刷新：A 是管理员、B 是普通用户，身份不能串
    await Promise.all([
      goto(cdp, pageA.sessionId, `${base}/`),
      goto(cdp, pageB.sessionId, `${base}/`),
    ])
    const [reloadA, reloadB] = await Promise.all([
      apiJson(cdp, pageA.sessionId, '/api/auth/me'),
      apiJson(cdp, pageB.sessionId, '/api/auth/me'),
    ])

    // A 退出不得影响 B
    await apiJson(cdp, pageA.sessionId, '/api/auth/logout', { method: 'POST' })
    const bStillLoggedIn = await apiJson(cdp, pageB.sessionId, '/api/auth/me')
    const bStateAfterALogout = await pageState(cdp, pageB.sessionId)

    // A 切到 B：自选不得残留 A 的数据
    await apiLogin(cdp, pageA.sessionId, USER_B)
    await goto(cdp, pageA.sessionId, `${base}/watchlist`)
    await sleep(800)
    const watchlistA = await apiJson(cdp, pageA.sessionId, '/api/watchlist')
    const switchState = await pageState(cdp, pageA.sessionId)

    await screenshot(cdp, pageA.sessionId, outDir, 'e13-context-a')
    await screenshot(cdp, pageB.sessionId, outDir, 'e13-context-b')

    result.contexts = {
      A: { login: meA.username, state: stateA, me: meAAfter.json },
      B: { login: meB.username, state: stateB, me: meBAfter.json },
    }
    result.logout_isolation = {
      b_still_authenticated: bStillLoggedIn.status === 200,
      b_state_after_a_logout: bStateAfterALogout,
    }
    result.account_switch = {
      watchlist_status: watchlistA.status,
      watchlist_empty: Array.isArray(watchlistA.json) && watchlistA.json.length === 0,
      state: switchState,
    }
    result.parallel_reload = {
      a: reloadA.json?.username ?? null,
      b: reloadB.json?.username ?? null,
      isolated:
        reloadA.json?.username === USER_A.username &&
        reloadB.json?.username === USER_B.username,
    }
    result.errors = { a: pageA.errors, b: pageB.errors }
    result.pass =
      stateA.hasLoginForm === false &&
      stateB.hasLoginForm === false &&
      stateA.bodyLen > 200 &&
      stateB.bodyLen > 200 &&
      bStillLoggedIn.status === 200 &&
      result.account_switch.watchlist_empty &&
      result.parallel_reload.isolated
  } catch (error) {
    result.error = String(error)
    result.pass = false
  } finally {
    await closeContext(cdp, browserContextA)
    await closeContext(cdp, browserContextB)
  }
  return result
}

async function runE15(cdp, base, dist, outDir) {
  const browserContextId = (await cdp.send('Target.createBrowserContext')).browserContextId
  const page = await openPage(cdp, browserContextId)
  const result = { scenario: 'E15 Chunk 404', pass: false }

  try {
    await cdp.send(
      'Emulation.setDeviceMetricsOverride',
      { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false },
      page.sessionId,
    )
    await uiLogin(cdp, page.sessionId, base, USER_A)
    const chunk = readdirSync(join(dist, 'assets')).find(
      (name) => name.startsWith('Research-') && name.endsWith('.js'),
    )
    if (!chunk) throw new Error('未找到 Research lazy chunk')
    result.chunk = chunk
    result.chunk_url = `${base}/assets/${chunk}`

    let failUntil = Date.now() + 6000
    let failed = 0
    let passed = 0
    cdp.on('Fetch.requestPaused', (message) => {
      if (message.sessionId !== page.sessionId) return
      const requestId = message.params.requestId
      if (Date.now() < failUntil) {
        failed += 1
        cdp
          .send('Fetch.failRequest', { requestId, errorReason: 'Failed' }, page.sessionId)
          .catch(() => {})
      } else {
        passed += 1
        cdp.send('Fetch.continueRequest', { requestId }, page.sessionId).catch(() => {})
      }
    })
    await cdp.send(
      'Fetch.enable',
      {
        patterns: [
          { urlPattern: `*assets/${chunk}*`, requestStage: 'Request' },
        ],
      },
      page.sessionId,
    )

    let navigations = 0
    cdp.on('Page.frameNavigated', (message) => {
      if (message.sessionId === page.sessionId) navigations += 1
    })

    // 直接导航到 lazy 路由（真实浏览器加载），避免依赖导航菜单的悬停/折叠状态
    await cdp.send('Page.navigate', { url: `${base}/research` }, page.sessionId)
    await sleep(7000)
    result.failed_requests = failed
    result.reload_navigations = navigations
    const afterFailure = await pageState(cdp, page.sessionId)
    result.after_failure = afterFailure
    await screenshot(cdp, page.sessionId, outDir, 'e15-chunk-404-fallback')

    // 恢复网络：点「重新加载」或直接导航
    failUntil = 0
    await sleep(300)
    const clickedReload = await evaluate(
      cdp,
      page.sessionId,
      `(() => { const b = Array.from(document.querySelectorAll('button')).find((x) => (x.textContent||'').includes('重新加载')); if (b) { b.click(); return true } return false })()`,
    )
    if (!clickedReload) await goto(cdp, page.sessionId, `${base}/research`)
    await waitUntil(
      cdp,
      page.sessionId,
      `location.pathname === '/research' && document.body.innerText.length > 200 && !document.body.innerText.includes('页面加载失败')`,
      20000,
      '恢复后页面可见',
    )
    const recovered = await pageState(cdp, page.sessionId)
    await screenshot(cdp, page.sessionId, outDir, 'e15-chunk-404-recovered')

    result.reload_clicked = clickedReload
    result.recovered = recovered
    result.errors = page.errors.filter((entry) => !entry.text.includes('E2E_RENDER_ERROR_FIXTURE'))
    result.pass =
      failed >= 1 &&
      passed >= 1 &&
      (afterFailure.hasFallback || afterFailure.rootChildren > 0) &&
      afterFailure.bodyLen > 0 &&
      recovered.bodyLen > 200 &&
      !recovered.hasFallback
  } catch (error) {
    result.error = String(error)
    result.pass = false
  } finally {
    await closeContext(cdp, browserContextId)
  }
  return result
}

async function runE16(cdp, devBase, outDir) {
  const browserContextId = (await cdp.send('Target.createBrowserContext')).browserContextId
  const page = await openPage(cdp, browserContextId)
  const result = { scenario: 'E16 React 渲染异常', pass: false }
  try {
    await goto(cdp, page.sessionId, `${devBase}/__e2e/render-error`)
    await waitUntil(
      cdp,
      page.sessionId,
      `document.body.innerText.includes('页面加载失败')`,
      15000,
      'ErrorBoundary fallback',
    )
    const fallback = await pageState(cdp, page.sessionId)
    await screenshot(cdp, page.sessionId, outDir, 'e16-render-error')

    await goto(cdp, page.sessionId, `${devBase}/login`)
    const loginPage = await pageState(cdp, page.sessionId)

    result.fallback = fallback
    result.other_route = loginPage
    result.errors = page.errors
    result.pass =
      fallback.hasFallback &&
      fallback.bodyLen > 20 &&
      fallback.rootChildren > 0 &&
      loginPage.hasLoginForm
  } catch (error) {
    result.error = String(error)
    result.pass = false
  } finally {
    await closeContext(cdp, browserContextId)
  }
  return result
}

async function clickLogout(cdp, sessionId) {
  const clicked = await evaluate(
    cdp,
    sessionId,
    `(() => {
      const button = Array.from(document.querySelectorAll('button')).find(
        (b) => (b.textContent || '').trim() === '退出',
      )
      if (button) { button.click(); return 'button' }
      return 'none'
    })()`,
  )
  if (clicked === 'button') {
    await sleep(800)
  } else {
    await apiJson(cdp, sessionId, '/api/auth/logout', { method: 'POST' })
    await cdp.send('Page.reload', {}, sessionId).catch(() => {})
    await sleep(800)
  }
  return clicked
}

async function runViewports(cdp, base, devBase, outDir) {
  const sizes = [
    { name: '1280x800', width: 1280, height: 800 },
    { name: '1920x1080', width: 1920, height: 1080 },
    { name: '390x844', width: 390, height: 844 },
  ]
  const browserContextId = (await cdp.send('Target.createBrowserContext')).browserContextId
  const page = await openPage(cdp, browserContextId)
  const result = { scenario: '三视口', sizes: {} }

  try {
    for (const size of sizes) {
      await cdp.send(
        'Emulation.setDeviceMetricsOverride',
        {
          width: size.width,
          height: size.height,
          deviceScaleFactor: 1,
          mobile: size.width < 600,
        },
        page.sessionId,
      )
      await goto(cdp, page.sessionId, `${base}/login`)
      const loginState = await pageState(cdp, page.sessionId)
      await uiLogin(cdp, page.sessionId, base, USER_A)
      const homeState = await pageState(cdp, page.sessionId)
      const menuVisible = await evaluate(
        cdp,
        page.sessionId,
        `(() => {
          const menu = Array.from(document.querySelectorAll('button')).find(
            (b) => (b.textContent || '').includes('打开导航菜单'),
          )
          if (menu) { menu.click(); return true }
          return false
        })()`,
      )
      await sleep(400)
      const navLinks = await evaluate(
        cdp,
        page.sessionId,
        `Array.from(document.querySelectorAll('a')).filter((a) => a.offsetParent !== null).length`,
      )
      await screenshot(cdp, page.sessionId, outDir, `viewport-${size.name}-home`)
      const logoutMethod = await clickLogout(cdp, page.sessionId)
      await waitUntil(
        cdp,
        page.sessionId,
        `!!document.querySelector('#username')`,
        15000,
        `${size.name} 退出后回到登录页`,
      )
      const logoutState = await pageState(cdp, page.sessionId)

      // DEV-only 错误 fallback 在同一视口验证
      await goto(cdp, page.sessionId, `${devBase}/__e2e/render-error`)
      await waitUntil(
        cdp,
        page.sessionId,
        `document.body.innerText.includes('页面加载失败')`,
        15000,
        `${size.name} 错误 fallback`,
      )
      const fallbackState = await pageState(cdp, page.sessionId)
      await screenshot(cdp, page.sessionId, outDir, `viewport-${size.name}-fallback`)

      result.sizes[size.name] = {
        requested: { width: size.width, height: size.height },
        actual: { width: homeState.innerWidth, height: homeState.innerHeight },
        login: loginState,
        home: homeState,
        nav: { menu_clicked: menuVisible, visible_links: navLinks },
        logout: { method: logoutMethod, state: logoutState },
        fallback: fallbackState,
        overflow: {
          login: overflow(loginState),
          home: overflow(homeState),
          fallback: overflow(fallbackState),
        },
      }
    }
    result.pass = Object.values(result.sizes).every(
      (entry) =>
        entry.login.hasLoginForm &&
        entry.home.bodyLen > 200 &&
        entry.home.rootChildren > 0 &&
        entry.nav.visible_links >= 3 &&
        entry.logout.state.hasLoginForm &&
        entry.fallback.hasFallback &&
        !entry.overflow.login &&
        !entry.overflow.home &&
        !entry.overflow.fallback,
    )
    result.errors = page.errors
  } catch (error) {
    result.error = String(error)
    result.pass = false
  } finally {
    await closeContext(cdp, browserContextId)
  }
  return result
}

async function runHistory(cdp, base) {
  // S6.10：深层链接 + 刷新 + 浏览器前进/后退。只依赖 --base（生产构建由后端直接托管）。
  const browserContextId = (await cdp.send('Target.createBrowserContext')).browserContextId
  const page = await openPage(cdp, browserContextId)
  const sessionId = page.sessionId
  const result = { scenario: '深层链接 / 刷新 / 前进后退', pass: false, steps: {} }
  try {
    await cdp.send(
      'Emulation.setDeviceMetricsOverride',
      { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false },
      sessionId,
    )
    await uiLogin(cdp, sessionId, base, USER_A)
    await waitUntil(cdp, sessionId, `document.querySelectorAll('a').length >= 5`, 15000, '首页导航渲染')
    result.steps.home = await pageState(cdp, sessionId)

    // 1) 深层链接：未经首页中转，直接打开 /research
    await goto(cdp, sessionId, `${base}/research`)
    await waitUntil(
      cdp,
      sessionId,
      `location.pathname === '/research' && document.querySelector('#root').childElementCount > 0 && !document.body.innerText.includes('页面加载失败') && !document.body.innerText.includes('加载中')`,
      25000,
      '深层链接 /research 渲染完成',
    )
    result.steps.deep_link = await pageState(cdp, sessionId)

    // 2) 刷新：同一路由重载后仍可渲染（不是靠内存里的 SPA 状态）
    await cdp.send('Page.reload', { ignoreCache: false }, sessionId)
    await sleep(800)
    await waitUntil(
      cdp,
      sessionId,
      `document.readyState === 'complete' && location.pathname === '/research' && document.body.innerText.length > 200 && !document.body.innerText.includes('页面加载失败') && !document.body.innerText.includes('加载中')`,
      25000,
      '刷新 /research',
    )
    result.steps.refresh = await pageState(cdp, sessionId)

    // 3) 站内导航到 /watchlist（优先真实链接点击；导航里没有该 <a> 时退回 pushState）
    const navInfo = await evaluate(
      cdp,
      sessionId,
      `(() => {
        const anchors = Array.from(document.querySelectorAll('a'))
        const hrefs = anchors.map((a) => a.getAttribute('href')).filter(Boolean)
        const link = anchors.find((a) => {
          const href = a.getAttribute('href') || ''
          return href === '/watchlist' || href === '/watchlist/'
        })
        if (link) {
          link.click()
          return { clicked: true, anchors: hrefs.slice(0, 40) }
        }
        return { clicked: false, anchors: hrefs.slice(0, 40) }
      })()`,
    )
    result.nav_link_clicked = navInfo.clicked
    result.available_links = navInfo.anchors
    if (!navInfo.clicked) {
      // 真实链接不存在时用 pushState + popstate 走同一套 SPA 路由，
      // 仍然产生真实 history entry，供后面的 back/forward 使用。
      await evaluate(
        cdp,
        sessionId,
        `(() => {
          history.pushState({}, '', '/watchlist')
          window.dispatchEvent(new PopStateEvent('popstate', { state: {} }))
          return true
        })()`,
      )
    }
    await waitUntil(
      cdp,
      sessionId,
      `location.pathname === '/watchlist' && document.body.innerText.includes('自选池')`,
      15000,
      '站内导航 /watchlist',
    )
    result.steps.nav_watchlist = await pageState(cdp, sessionId)

    // 4) 浏览器后退 → 回到 /research
    await evaluate(cdp, sessionId, 'history.back()')
    await waitUntil(
      cdp,
      sessionId,
      `location.pathname === '/research' && document.body.innerText.includes('研究假设')`,
      15000,
      'history.back → /research',
    )
    result.steps.back = await pageState(cdp, sessionId)

    // 5) 浏览器前进 → 回到 /watchlist
    await evaluate(cdp, sessionId, 'history.forward()')
    await waitUntil(
      cdp,
      sessionId,
      `location.pathname === '/watchlist' && document.body.innerText.includes('自选池')`,
      15000,
      'history.forward → /watchlist',
    )
    result.steps.forward = await pageState(cdp, sessionId)

    result.pass =
      result.steps.home.bodyLen > 200 &&
      result.steps.deep_link.path === '/research' &&
      result.steps.deep_link.bodyLen > 200 &&
      result.steps.refresh.path === '/research' &&
      result.steps.refresh.bodyLen > 200 &&
      result.steps.nav_watchlist.path === '/watchlist' &&
      result.steps.nav_watchlist.bodyLen > 50 &&
      result.steps.back.path === '/research' &&
      result.steps.back.bodyLen > 200 &&
      result.steps.forward.path === '/watchlist' &&
      result.steps.forward.bodyLen > 50 &&
      !result.steps.deep_link.hasFallback &&
      !result.steps.refresh.hasFallback &&
      !result.steps.back.hasFallback &&
      !result.steps.forward.hasFallback
    result.errors = page.errors
  } catch (error) {
    result.error = String(error)
    result.pass = false
  } finally {
    await closeContext(cdp, browserContextId)
  }
  return result
}

async function main() {
  const args = parseArgs()
  mkdirSync(join(args.out, 'screenshots'), { recursive: true })
  const profile = join(tmpdir(), `s6_8_cdp_${Date.now()}`)
  const chrome = spawn(
    CHROME,
    [
      '--headless=new',
      `--remote-debugging-port=${CDP_PORT}`,
      `--user-data-dir=${profile}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-gpu',
      '--hide-scrollbars',
      '--no-sandbox',
      'about:blank',
    ],
    { stdio: 'ignore' },
  )

  const cdp = new CDP((await waitForJson(`${CDP_URL}/json/version`)).webSocketDebuggerUrl)
  await cdp.connect()
  const summary = {
    generated_at: new Date().toISOString(),
    base: args.base,
    dev_base: args.devBase,
    chrome: CHROME,
    results: {},
  }
  try {
    if (args.only === 'history') {
      summary.results.history = await runHistory(cdp, args.base)
    } else {
      summary.results.e13 = await runE13(cdp, args.base, args.out)
      summary.results.e15 = await runE15(cdp, args.base, args.dist, args.out)
      summary.results.e16 = await runE16(cdp, args.devBase, args.out)
      summary.results.viewports = await runViewports(cdp, args.base, args.devBase, args.out)
    }
  } finally {
    cdp.close()
    chrome.kill()
    try {
      rmSync(profile, { recursive: true, force: true })
    } catch {
      // Windows 上 Chrome 可能还占着文件，忽略
    }
  }

  const files =
    args.only === 'history'
      ? { 'history_navigation.json': summary.results.history }
      : {
          'e13_context_isolation.json': summary.results.e13,
          'e15_chunk_404.json': summary.results.e15,
          'e16_render_error.json': summary.results.e16,
          'viewports.json': summary.results.viewports,
        }
  for (const [name, payload] of Object.entries(files)) {
    writeFileSync(join(args.out, name), JSON.stringify(payload, null, 2), 'utf-8')
  }
  writeFileSync(join(args.out, 'summary.json'), JSON.stringify(summary, null, 2), 'utf-8')

  const statuses = Object.fromEntries(
    Object.entries(summary.results).map(([key, value]) => [key, value.pass === true]),
  )
  console.log(JSON.stringify({ statuses, files: Object.keys(files) }, null, 2))
  if (Object.values(statuses).some((value) => value !== true)) process.exitCode = 1
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
