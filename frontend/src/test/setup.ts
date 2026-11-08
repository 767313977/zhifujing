import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// React 19 的 act 需要这个开关；不设会刷 "not configured to support act" 警告
;(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true

afterEach(() => {
  cleanup()
  try {
    window.localStorage.clear()
  } catch {
    // 个别用例故意把 localStorage 的 getter 打成抛异常，这里忽略即可
  }
})
