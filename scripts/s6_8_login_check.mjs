/**
 * S6.8 B 包：单版本登录 smoke（真实 Chrome / CDP）。
 *
 * 用法：
 *   node scripts/s6_8_login_check.mjs --base http://127.0.0.1:8012 --expect success|blank
 *
 * - success：登录 → 首页可见 → 刷新 → 深层链接 → 退出回登录页；
 * - blank：旧版已知缺陷对照，期望登录后停在 /login 且 #root 为空。
 *
 * 只输出状态码/DOM 读数，不记录 Cookie/密码。
 */

import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const PORT = 9334

const args = process.argv.slice(2)
function arg(name, fallback) {
  const index = args.indexOf(`--${name}`)
  return index >= 0 ? args[index + 1] : fallback
}
const BASE = arg('base', 'http://127.0.0.1:8012')
const EXPECT = arg('expect', 'success')
const USER = arg('user', 's7staging')
const PASSWORD = arg('password', 'staging-password-123')
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

class CDP {
  constructor(url) {
    this.url = url
    this.id = 1
    this.pending = new Map()
    this.listeners = new Map()
  }
  async connect() {
    this.ws = new WebSocket(this.url)
    await new Promise((resolve, reject) => {
      this.ws.addEventListener('open', resolve, { once: true })
      this.ws.addEventListener('error', reject, { once: true })
    })
    this.ws.addEventListener('message', (event) => {
      const message = JSON.parse(event.data)
      if (message.id) {
        const task = this.pending.get(message.id)
        if (!task) return
        this.pending.delete(message.id)
        message.error ? task.reject(new Error(message.error.message)) : task.resolve(message.result)
        return
      }
      for (const handler of this.listeners.get(message.method) ?? []) handler(message)
    })
  }
  send(method, params = {}, sessionId) {
    const id = this.id++
    const payload = { id, method, params }
    if (sessionId) payload.sessionId = sessionId
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.ws.send(JSON.stringify(payload))
    })
  }
  on(method, handler) {
    if (!this.listeners.has(method)) this.listeners.set(method, [])
    this.listeners.get(method).push(handler)
  }
}

async function waitJson(url, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url)
      if (response.ok) return response.json()
    } catch {
      // waiting
    }
    await sleep(200)
  }
  throw new Error('等待 Chrome CDP 超时')
}

async function evaluate(cdp, sid, expression) {
  const result = await cdp.send(
    'Runtime.evaluate',
    { expression, awaitPromise: true, returnByValue: true, userGesture: true },
    sid,
  )
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text)
  return result.result?.value
}

async function waitUntil(cdp, sid, expression, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      if (await evaluate(cdp, sid, expression)) return true
    } catch {
      // navigation in progress
    }
    await sleep(150)
  }
  throw new Error(`等待超时：${label}`)
}

async function goto(cdp, sid, url) {
  await cdp.send('Page.navigate', { url }, sid)
  await waitUntil(cdp, sid, `document.readyState === 'complete'`, 20000, `goto ${url}`)
  await sleep(300)
}

const STATE = `(() => {
  const root = document.querySelector('#root')
  return {
    path: location.pathname,
    rootChildren: root ? root.childElementCount : -1,
    bodyLen: document.body.innerText.length,
    hasLoginForm: !!document.querySelector('#username'),
    hasExit: Array.from(document.querySelectorAll('button')).some((b) => (b.textContent || '').trim() === '退出'),
    textHead: document.body.innerText.slice(0, 120),
  }
})()`

async function main() {
  const profile = mkdtempSync(join(tmpdir(), 's6_8_login_'))
  const chrome = spawn(
    CHROME,
    [
      '--headless=new',
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${profile}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-gpu',
      '--no-sandbox',
      'about:blank',
    ],
    { stdio: 'ignore' },
  )
  const cdp = new CDP((await waitJson(`http://127.0.0.1:${PORT}/json/version`)).webSocketDebuggerUrl)
  await cdp.connect()
  const output = { base: BASE, expect: EXPECT, steps: [] }
  try {
    const target = await cdp.send('Target.createTarget', { url: 'about:blank' })
    const attached = await cdp.send('Target.attachToTarget', {
      targetId: target.targetId,
      flatten: true,
    })
    const sid = attached.sessionId
    await cdp.send('Page.enable', {}, sid)
    await cdp.send('Runtime.enable', {}, sid)
    await cdp.send(
      'Emulation.setDeviceMetricsOverride',
      { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false },
      sid,
    )

    await goto(cdp, sid, `${BASE}/login`)
    await waitUntil(cdp, sid, `!!document.querySelector('#username')`, 15000, '登录表单')
    await evaluate(
      cdp,
      sid,
      `(() => {
        const set = (el, value) => {
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, value)
          el.dispatchEvent(new Event('input', { bubbles: true }))
        }
        set(document.querySelector('#username'), ${JSON.stringify(USER)})
        set(document.querySelector('#password'), ${JSON.stringify(PASSWORD)})
        document.querySelector('button[type="submit"]').click()
        return true
      })()`,
    )
    await sleep(8000)
    const afterLogin = await evaluate(cdp, sid, STATE)
    output.steps.push({ step: 'login', state: afterLogin })

    if (EXPECT === 'success') {
      await cdp.send('Page.reload', {}, sid)
      await sleep(2500)
      const afterRefresh = await evaluate(cdp, sid, STATE)
      output.steps.push({ step: 'refresh', state: afterRefresh })

      await goto(cdp, sid, `${BASE}/limit-up`)
      await sleep(1500)
      const deepLink = await evaluate(cdp, sid, STATE)
      output.steps.push({ step: 'deep_link', state: deepLink })

      await evaluate(
        cdp,
        sid,
        `fetch('/api/auth/logout', { method: 'POST' }).then((r) => r.status)`,
      )
      await cdp.send('Page.reload', {}, sid)
      await sleep(1500)
      const afterLogout = await evaluate(cdp, sid, STATE)
      output.steps.push({ step: 'logout', state: afterLogout })

      output.pass =
        afterLogin.path === '/' &&
        afterLogin.rootChildren > 0 &&
        afterLogin.bodyLen > 200 &&
        afterRefresh.rootChildren > 0 &&
        afterRefresh.bodyLen > 200 &&
        deepLink.path === '/limit-up' &&
        deepLink.bodyLen > 200 &&
        afterLogout.hasLoginForm
    } else {
      output.pass =
        afterLogin.path === '/login' &&
        afterLogin.rootChildren === 0 &&
        afterLogin.bodyLen === 0
    }
  } catch (error) {
    output.error = String(error)
    output.pass = false
  } finally {
    try {
      cdp.ws?.close()
    } catch {
      // ignore
    }
    chrome.kill()
    try {
      rmSync(profile, { recursive: true, force: true })
    } catch {
      // ignore
    }
  }
  console.log(JSON.stringify(output, null, 2))
  process.exitCode = output.pass ? 0 : 1
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
