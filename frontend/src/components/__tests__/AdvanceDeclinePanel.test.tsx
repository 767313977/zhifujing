import { render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'

vi.mock('../../api/client', () => {
  class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  }
  return {
    api: {
      advanceDecline: vi.fn().mockRejectedValue(new ApiError(404, '库里还没有个股日线')),
    },
    isNoDataError: (err: unknown) => err instanceof ApiError && err.status === 404,
  }
})

import AdvanceDeclinePanel from '../AdvanceDeclinePanel'

test('404 空态必须结束 loading 并显示暂无数据', async () => {
  render(<AdvanceDeclinePanel />)
  // 回归：S6.5 空库验收发现 404 被当空态后 error=null，loading 永真、空态永不显示
  expect(await screen.findByText('暂无数据')).toBeTruthy()
  expect(screen.queryByText('加载中…')).toBeNull()
})
