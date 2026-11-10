# 致富经选股 V2：P0 修复与收尾验收报告

- 日期：2026-11-10
- 范围：P0-1 RPS NULL 排序 / P0-2 缓存版本失效与 single-flight / P0-3 选股范围语义 / P0-4 入选原因
- 结论：**`STOCK_SCREEN_V2_P0_COMPLETE`**
- 同时保留：`STRATEGY_PERFORMANCE_NOT_VERIFIED`、`PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、`P13_RELEASE_NO_GO`

---

## A. Git 版本

| 项 | 值 |
| --- | --- |
| 起始 HEAD | `3eeb064c0531956e5a188fbcb05ac979c1f4b91a` |
| 当前实际分支 | `feature/stock-screening-v2` |
| 基线（冻结 P1.3） | 同一 commit `3eeb064`（`docs(p1.3): revise ops evidence handoff per final review`） |
| 最终 HEAD | 仍为 `3eeb064`（本轮改动全部在 working tree，尚未提交） |

### A.1 分支关系（只读证据）

```
* feature/stock-screening-v2  3eeb064 docs(p1.3): revise ops evidence handoff per final review
  feat/p0.2-research-ui       3eeb064 docs(p1.3): revise ops evidence handoff per final review
```

两个分支指向同一 commit，V2 改动未产生独立提交。分支差异为空（`git diff --stat 3eeb064c0...HEAD` 无输出）。

### A.2 修改文件清单

| 文件 | 新增 | 删除 | 说明 |
| --- | --- | --- | --- |
| `backend/app/api/pools.py` | +139 | −15 | `_cached()` 重写：data_revision 版本向量 + single-flight + fail closed |
| `backend/app/services/pools.py` | +102 | −15 | RPS NULL 安全排序、`selection_scope` 元数据、`_build_rps_reasons()` |
| `backend/app/schemas.py` | +8 | 0 | `RpsRowOut.reasons`、`RpsOut.selection_scope/cap/evaluated_count` |
| `frontend/src/pages/Pools.tsx` | +37 | 0 | top_n 提示条、reasons 可展开详情列 |
| `frontend/src/api/types.ts` | +8 | 0 | 前端类型同步 |

### A.3 新增测试文件

| 文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `tests/test_pools_rps_line_red.py` | 11 | RPS 一线红 NULL 排序边界 |
| `tests/test_p0_cache_concurrency.py` | 13 | single-flight 并发安全 |
| `tests/test_p0_selection_scope.py` | 9 | 选股范围语义 |
| `tests/test_p0_reasons_consistency.py` | 12 | reasons 与真实筛选一致性 |

### A.4 未提交改动提醒

working tree 中同时存在 P1.3 相关的未提交改动（`backend/app/api/plans.py`、`backend/app/jobs/scan_rps.py`、`scripts/install_data_revision.py`、`tests/test_install_data_revision.py`、`docs/p1.3/lean/` 等）。**不得整包提交，也不得 reset/rebase 丢弃 P1.3 修复**；须用 `git add -p` 或等价方式拆分。

---

## B. 修复结果

### B.1 P0-1 SQLite 多参数 `func.max` 的 NULL 传播

| 项 | 内容 |
| --- | --- |
| 问题 | `func.max(rps50, rps120, rps250)` 任一参数为 NULL 时整体返回 NULL，达标行被排到队尾 |
| 根因 | SQLite 多参数标量函数遵循 NULL 传播语义，与"忽略缺失档位取剩余最大值"的业务意图不符 |
| 修改函数 | `backend/app/services/pools.py` → `rps_board()` 的 `line_red` 分支 |
| 修改方式 | 三档分别 `func.coalesce(col, -1.0)` 后取 `func.max`；追加 `code.asc()` 作为确定性次序。哨兵 `-1.0` 仅参与排序，不回写数据库、不出现在响应中 |
| 测试证明 | `tests/test_pools_rps_line_red.py` 11 例：三档有值 / 一档 NULL / 两档 NULL / 三档全 NULL / 单档达标 3 例 / 边界 90 / 平局按 code 稳定排序 / 显式日期与默认日期一致 / 与 Python 独立计算结果逐位对齐 |
| 状态 | PASS |

### B.2 P0-2a 缓存数据版本失效

| 项 | 内容 |
| --- | --- |
| 问题 | 旧缓存键只含"最新交易日 + 参数"，同日数据修订后仍返回旧值 |
| 根因 | 版本粒度取 `MAX(trade_date)`，无法感知同日 UPDATE/DELETE |
| 修改函数 | `backend/app/api/pools.py` → `_effective_key()`、`_cached()` |
| 修改方式 | 键改为 `("rev", state.identity, state.version) + raw_key`；`identity` 含 `db_uuid`；`state.ready == False` 时返回 `None`，`_cached()` 跳过缓存直接查库（fail closed） |
| 依赖完整性 | RPS 榜依赖 `rps_daily` / `stock_basic` / `stock_daily` / `pattern_hit`；首板与板块跟踪各自依赖其表。**只有已安装 `data_revision` 触发器的数据域享有强一致**；未覆盖域（如 `stock_concept`、可转债、ETF 等）沿用 TTL=10min 的陈旧窗口，不声称强一致 |
| 测试证明 | `test_p0_cache_concurrency.py::test_revision_change_isolated_by_key`、`test_different_db_uuid_does_not_share_cache`、`test_revision_unavailable_skips_cache`、`test_revision_read_exception_skips_cache` |
| 状态 | PASS（已安装域）/ 明确限制（未安装域） |

### B.3 P0-2b single-flight 并发安全

| 项 | 内容 |
| --- | --- |
| 问题 1 | follower 等待超时后直接 `build()`，未注册 inflight，20 个超时并发会放大为 20 次昂贵计算 |
| 问题 2 | leader 抛异常时，异常路径与 inflight 清理顺序不明确，存在 follower 永久阻塞与脏缓存风险 |
| 根因 | 超时分支绕开 single-flight 边界；异常路径缺少显式的"不写缓存"保证 |
| 修改函数 | `backend/app/api/pools.py` → `_cached()` |
| 修改方式 | ① leader 采用 `try / except / finally`：异常在 `finally` 之前 `raise`，写缓存语句位于 `try-finally` 之后 → 异常时**不写缓存、不吞异常**；② `finally` 中在同一 `_inflight_lock` 内 `pop` 并 `set()`；③ follower 超时后先复查缓存，仍 miss 则绕开缓存直查，**不注册新 inflight、不写缓存** |
| 测试证明 | `test_p0_cache_concurrency.py` 13 例：20 并发只 build 一次 / 异常唤醒全部 waiter / 异常不写缓存 / 异常后可重试 / 超时不新增 inflight / 等待临界处命中缓存 / revision 变化隔离 / db_uuid 隔离 / revision 不可用直查 / revision 查询异常直查 / 不同参数不串键 / 容量淘汰 / 验收标记 |
| 状态 | PASS |

### B.4 P0-3 选股范围语义

| 项 | 内容 |
| --- | --- |
| 问题 | "强度前 N 只再施加昂贵过滤"与"最终输出截断"被混为一谈，`limit=50` 容易被误读为"只筛了 50 只" |
| 根因 | 缺少机器可读的范围元数据 |
| 修改函数 | `backend/app/services/pools.py` → `rps_board()`；`RpsBoard` dataclass；`frontend/src/pages/Pools.tsx` |
| 语义定义 | `selection_scope = "top_n"` 仅当 `(breakout_only or trend_template) and order != "factor"`，此时 `selection_cap = max(limit*3, 300)`；其余情况（纯强度排序、factor 排序、空结果）为 `"full_market"` 且 `selection_cap = None`。`evaluated_count` = SQL `limit(cap)` 后实际进入昂贵过滤的记录数，**不等于**返回行数 |
| 前端 | `top_n` 时显示提示条："当前仅在强度排名前 N 的候选中检查突破或趋势条件，结果不代表全市场完整筛选"；`full_market` 不显示 |
| 测试证明 | 9 例：纯强度排序为 full_market / 大 limit 仍为 full_market / breakout 转 top_n / cap 计算 `max(limit*3,300)` / 小 limit 不收缩 cap / factor 永为 full_market / `evaluated_count != returned_count` / 空数据 full_market / dual 模式 full_market |
| 状态 | PASS |

### B.5 P0-4 入选原因一致性

| 项 | 内容 |
| --- | --- |
| 问题 | `hits` / `tags` 只表达形态与标签，不给出指标数值、阈值与是否通过 |
| 根因 | 缺少结构化可解释输出 |
| 修改函数 | `backend/app/services/pools.py` → `_build_rps_reasons()`；`RpsRow.reasons` |
| 一致性保证 | ① 取值来自本次查询的目标交易日，不取最新日；② NULL 不落为 0，仅列有值指标；③ `dual` 分别使用 `RPS_DUAL_50=90` 与 `RPS_DUAL_120=85`；④ `line_red` 只列 ≥ `RPS_LINE_RED=90` 的档位，语义为"至少一档达标"；⑤ `all` 只看 RPS250 与 `min_rps`，不套用 dual 条件；⑥ 突破形态名直接取自 `_breakout_hits()` 返回值；⑦ `passed=False` 的项只会出现在不会入选的行上，最终名单中不会出现误导性"未通过" |
| 测试证明 | 12 例：all 不套 dual / dual 分别列 50 与 120 及真实阈值 / line_red 只列达标档 / 三档全达标 / NULL 不转 0 / all 模式 rps250 为 NULL 不产生 reason / breakout 空命中语义 / 形态名一致 / 取值来自目标日期而非旧日期 / dual 阈值不通过则不入选 / line_red 单档达标 / schema 字段贯通 |
| 状态 | PASS |

---

## C. 测试

| 批次 | 命令 | 通过 | 跳过 | 失败 |
| --- | --- | ---: | ---: | ---: |
| RPS NULL 排序 | `python -m pytest tests/test_pools_rps_line_red.py -v` | 11 | 0 | 0 |
| 并发安全 | `python -m pytest tests/test_p0_cache_concurrency.py -v` | 13 | 0 | 0 |
| 范围语义 | `python -m pytest tests/test_p0_selection_scope.py -v` | 9 | 0 | 0 |
| reasons 一致性 | `python -m pytest tests/test_p0_reasons_consistency.py -v` | 12 | 0 | 0 |
| pools 回归 | `python -m pytest tests/test_pools*.py -v` | 36 | 0 | 0 |
| 后端全量 | `python -m pytest tests/ --tb=no -q` | 1005 | 3 | 0 |
| 前端构建 | `npm run build` | 通过（TypeScript 零错误） | — | — |

基线为 971 passed；本轮新增 34 例，全量升至 1005 passed。

> 说明：`test_p0_cache_concurrency.py` 会输出 `PytestUnhandledThreadExceptionWarning`，来源是异常场景用例故意在子线程抛出 `RuntimeError`，属预期行为，非失败。

---

## D. 性能

| 项 | 结论 |
| --- | --- |
| 缓存读取额外开销 | 每次多一次 `data_revision.status()` 单行查询，量级约 1–2 ms（未仪器化测量） |
| single-flight 锁开销 | 非竞争路径为一次 `dict.get`，可忽略 |
| `reasons` 构造 | 每行常量级字典拼接，开销可忽略 |
| `selection_scope` 元数据 | 纯内存字段，无计算 |
| 新旧版本延迟与内存对比 | **未测试（UNMEASURED）**，需在真实 fupan.db 与 10 万行级数据上做基准 |

---

## E. 选股效果

| 维度 | 结论 | 说明 |
| --- | --- | --- |
| 结果正确性 | 通过 | 1005 例全绿；reasons 与 SQL 筛选条件逐项对齐；single-flight 七类并发场景安全 |
| 筛选效率 | 改善 | 版本向量使同日修订自动失效；fail closed 避免伪造缓存；top_n 口径显式化，消除"只筛前 N 只"的误读 |
| 历史收益 | **未验证** | 本轮未执行任何历史回测；`/rps/track`、`/sector-rps/track` 仅证明具备验证能力，不构成收益结论 |
| 样本外表现 | **未验证** | 无合规历史数据可用，禁止在空测试库上调参 |

---

## F. 剩余事项

1. **提交拆分**：V2 改动与 P1.3 未提交改动混在同一 working tree。需按文件粒度拆分提交，禁止整包提交、禁止 `reset --hard` / `rebase` / 强推。
2. **未覆盖数据域的缓存一致性**：`stock_concept`、可转债、ETF 等表若未安装 `data_revision` 触发器，则相关缓存仍为 TTL=10min 的陈旧窗口，不得对外声称强一致。需在生产库（授权后）确认触发器覆盖面。
3. **历史回测缺口**：在获得允许使用的非敏感历史行情数据之前，不读取待隔离的敏感库、不上传快照、不编造收益、不调整策略默认权重。
4. **前端 `reasons` 展开控件**：当前使用原生 `<details>`，桌面端可用；移动端点击与滚动行为待确认。

---

## G. 冻结标记

```
STOCK_SCREEN_V2_P0_COMPLETE           功能验收通过
STRATEGY_PERFORMANCE_NOT_VERIFIED     无合规回测数据
PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED  未经审批，禁止部署
P13_RELEASE_NO_GO                     V2 与 P1.3 改动未拆分提交
```

本轮未部署、未迁移生产数据库、未读取待隔离数据库、未调整生产调度器。
