# 致富经选股 V2：P0 收口与提交记录

- 日期：2026-11-10
- 结论：`STOCK_SCREEN_V2_P0_COMPLETE`、`GIT_COMMITS_SEPARATED_AND_VERIFIED`
- 保留：`STRATEGY_PERFORMANCE_NOT_VERIFIED`、`PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、`P13_RELEASE_NO_GO`

## 1. 提交

| 归属 | SHA | 说明 |
| --- | --- | --- |
| P1.3 | `b3539421aef71b17e243dcb8d151b9654d462e28` | RPS 回填写入加固、revision 触发器、指纹与只读审计 |
| V2 | `ac50e707497106046ddba82c7da0d94550a64dab` | RPS NULL 排序、缓存版本向量、single-flight 503、范围语义、reasons |
| 文档 | 见下方 `git log -1` | 本文件，纯文档提交 |

分支：`feature/stock-screening-v2`，基线 `3eeb064`。

## 2. Single-flight 超时处理

旧实现：follower 等待 60s 后直接 `build()`，被合并的请求在超时瞬间重新分裂为 N 份昂贵计算。

新实现（`backend/app/api/pools.py`）：

```python
event, is_leader = _register_or_join(final_key)
if is_leader:
    return _run_leader(final_key, event, build)

event.wait(_SINGLE_FLIGHT_TIMEOUT)

hit = _read_cache(final_key)
if hit is not _MISS:
    return hit

with _inflight_lock:
    still_running = final_key in _inflight

if still_running:
    raise _busy()          # HTTP 503 + Retry-After: 2，绝不自行 build

# leader 异常退出才允许一次补算，仍然只有一个赢家
retry_event, retry_is_leader = _register_or_join(final_key)
if retry_is_leader:
    return _run_leader(final_key, retry_event, build)
raise _busy()
```

配套：`_run_leader` 成功时**先写缓存再清 inflight**，保证被唤醒的 follower 一定命中；失败时不写缓存、异常原样上抛、inflight 仍清理。

## 3. all 模式 reasons 核对

对照冻结代码 `rps_board()` 的 `all` 分支，真实 SQL 为：

- `rps250 IS NOT NULL AND rps250 >= min_rps`
- `rps120 IS NOT NULL AND rps120 >= min_rps`

原 `_build_rps_reasons()` 只列 RPS250，缺 RPS120。已改为两档都列，阈值均为 `min_rps`；NULL 不落为 0。**筛选阈值与结果未改动**，仅补全说明。

## 4. 测试

| 批次 | 命令 | 通过 | 跳过 | 失败 |
| --- | --- | ---: | ---: | ---: |
| V2 定向 | `pytest tests/test_pools_rps_line_red.py tests/test_p0_cache_concurrency.py tests/test_p0_selection_scope.py tests/test_p0_reasons_consistency.py` | 49 | 0 | 0 |
| P1.3 定向 | `pytest tests/test_install_data_revision.py tests/test_p13_fingerprint.py tests/test_prod_readonly_audit.py tests/test_scan_rps.py tests/test_startup_schema.py tests/test_data_revision.py` | 151 | 3 | 0 |
| 后端全量 | `pytest tests/` | 1009 | 3 | 0 |
| 前端类型 | `npx tsc --noEmit` | exit 0 | — | 0 |
| 前端构建 | `npm run build` | 通过 | — | 0 |

均在两笔提交之后、工作区干净状态下复跑。

## 5. 异常告警

`PytestUnhandledThreadExceptionWarning` 已消除。子线程异常改为捕获到列表并由主线程断言类型与内容，全量输出中仅剩 SQLAlchemy `DeprecationWarning`（与本次无关）。

## 6. 未提交 / 阻断项

- 工作区干净（`git status --porcelain` 无输出）。
- `backend/app/api/plans.py`、`frontend/src/index.css` 曾显示修改标记，经 `git diff --raw` 与 `git add` 验证为 `core.autocrlf` 的 stat 噪声，无内容差异，未纳入任何提交。
- 未包含任何 `.db` / `.db-wal` / `.db-shm` / `.env` / 密钥文件。

## 7. 效果边界

- 结果正确性：通过（测试与 SQL 条件逐项对齐）。
- 筛选效率：超时不再放大并发；缓存随 revision 失效。
- 历史收益 / 样本外表现：**未验证**，本轮未执行回测。

## 8. 剩余事项

1. 未安装 `data_revision` 触发器的数据域（如 `stock_concept`）仍为 TTL=10min 陈旧窗口，不得声称强一致。
2. 历史回测需待授权的非敏感行情数据，暂不选参数、不改默认权重。
3. 前端 `reasons` 展开控件在移动端的交互待验证。
