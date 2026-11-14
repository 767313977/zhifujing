# S6.5：全量回归（本轮 clean HEAD）

- 执行时间：2026-11-15
- 分支：`release/s6-canary-hardening-v3`
- 证据：`pytest-full.log`、`frontend-test.log`、`frontend-lint.log`、`frontend-build.log`

## 1. 后端

| 命令 | 结果 |
| --- | --- |
| `pytest -q` | **1735 passed, 4 skipped**（363.49s） |
| `ruff check backend tests scripts` | All checks passed |
| `python -m compileall backend` | exit 0 |
| `git diff --check` | exit 0 |

4 个 skip 均为既有环境相关跳过（转发/依赖外部环境的用例），本轮未新增 skip。

## 2. 前端

| 命令 | 结果 |
| --- | --- |
| `npm test`（vitest run） | **154 passed / 16 files** |
| `npm run lint`（oxlint） | 0 warnings / 0 errors（133 files） |
| `npm run build`（tsc -b + vite build） | 成功（vite 8.3.8） |

`AdvanceDeclinePanel.test.tsx` 回归仍在（1 passed），`/sentiment` 404 空态不再永久 loading。

## 3. 浏览器

见 [browser.md](./browser.md)：大部分页面与接口通过，但**登录后 SPA 跳转白屏**
（可复现 3 次）判 FAIL。

## 4. 结论

```text
S6_FULL_REGRESSION_PASSED = FAIL（唯一阻塞项：登录后跳转白屏）
G8 = FAIL
```

后端与前端自动化测试本身全部通过；失败来自真实浏览器验收发现的 UI 缺陷。
