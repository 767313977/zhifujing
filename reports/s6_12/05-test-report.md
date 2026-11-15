# S6.12 测试报告

- 时间：2026-11-15
- 完整日志：`05-test-report.log`

## 1. 后端全量

```text
$ python -m pytest -q
1758 passed, 4 skipped, 43 warnings in 390.80s (0:06:30)
exit code = 0
```

- 之前 2 个失败测试已按新行为修正：
  - `test_board_members_falls_back_without_network`：纯读路径保留“离线验收模式”说明；
  - `test_stock_sync_guard`：改为断言 409 且**不调用采集器**。

## 2. 新增负向测试（`tests/test_s6_12_write_boundary.py`）

```text
7 passed in 1.21s
```

覆盖：两个 sync 接口 409、板块成员 GET 纯读、请求内禁止写事件缓存/用量、
管理员作用域可写、用户 A 不能删用户 B 的自选。

## 3. RPS 迁移演练

```text
python scripts/s6_12_rps_migration_drill.py
verdict = PASS (17/17 checks)
```

证据：`03-rps-migration-evidence.json` / `.md`。

## 4. 隔离 Staging 冒烟

```text
python scripts/s6_12_staging_smoke.py
verdict = PASS
health=200 login=200 market=200 stock=200 sector_members=200
watchlist_sync=409 stock_sync=409
研究表前后摘要一致 = True
```

证据：`05-staging-smoke.json`。

## 5. 前端

```text
npm run lint   → 0 warnings / 0 errors (139 files)
npm run test   → 20 files, 168 tests passed
npm run build  → ✓ built in 747ms
```

## 6. 静态检查

```text
python -m ruff check backend scripts tests/test_s6_12_write_boundary.py
All checks passed!
```

## 7. 生产公共页面（只读）

```text
GET https://zhifujing.top/        → 200
GET https://zhifujing.top/login   → 200（<title>致富经</title>）
GET https://zhifujing.top/api/health → 200
```

登录后页面/个股/板块验收需要真实账号，本轮 **NOT_VERIFIED**。

## 8. 测试产物

`reports/s6/s62/migration-report.json` 仍被 pytest 重写；按任务要求未删除/未提交。
