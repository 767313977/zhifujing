# 致富经 zhifujing — S5 验收报告

- 日期：2026-11-14（Asia/Shanghai）
- 仓库：`c:\Users\76731\Documents\trae_projects\fupan`
- 分支：`feature/stock-screening-v2`
- S4 基线：`7eaf798`（S4 代码）、`395ae3a`（S4 报告提交）
- S5 代码终稿：`428c26e`（D 包）；报告提交见 `git log` 最新 `docs(s5)`
- 工作区：提交后 clean，已推送 `origin/feature/stock-screening-v2`
- 裁定：`S5_INTEGRATED_CODE_READY_FOR_INDEPENDENT_REVIEW`

---

## 1. Git 与提交

| 提交 | 内容 |
| --- | --- |
| `395ae3a` | S4 基线（S4 报告） |
| `e6190c1` | `fix(research): enforce timestamp-aware cutoff evidence v3`（A） |
| `0d77b69` | `fix(quality): unify source-status and completeness contracts`（B） |
| `3a5f2e6` | `feat(research): add post-market research workbench`（C） |
| `8f7acc6` | `fix(ui): mount workbench inside app shell`（C 修正） |
| `428c26e` | `test(s5): harden offline egress and integrated release gates`（D） |
| `docs(s5)` | 本报告与 `reports/s5_*` 证据 |

- `git diff --name-status 395ae3a..428c26e` 未包含：生产 DB、`forward_c0/*`、C0 五脚本、`deploy/*`、调度配置；
- 只改 `backend/app/{services,api,schemas,external_guard,main}`、`sources/eastmoney.py`（仅加闸门）、`frontend/src/**`、`tests/**`、`scripts/**`、`reports/s5_*`、`S5-*.md`。

---

## 2. A — PIT v3（`e6190c1`）

### 2.1 规则

- 版本：`research-evidence-v3`；`cutoff=CLOSE` **明确对应观察日 15:00 Asia/Shanghai（含 15:00 整）**；
- 最早可用时刻 `known_at = max(observed_at, published_at)`；
- `known_at <= cutoff` → `CLOSE` → `AT_CUTOFF_KNOWN`；
- 同日但晚于 15:00 → `POST_CLOSE_UNKNOWN`（`AFTER_CUTOFF_SAME_DAY`）；
- D+1 → `LATER`；naive 且不信任 +08:00 约定 → `UNKNOWN`；来源未就绪 → `UNKNOWN`；
- `stock_daily` / `market_sentiment` 无首次入库时间 → `END_OF_DAY_FACT`，进 `UNKNOWN_OR_LATER`，**不再冒充 15:00 已知**；
- `sector_member` 同日一律 `SAME_DAY_UNKNOWN`；`stock_concept` 永久 `POST_CLOSE_UNKNOWN`；
- 三桶互斥、守恒由 `validate_time_bucket_invariants` 断言；兼容字段 `as_known_on_day` 只含严格已知子集。

### 2.2 时间例证（真实函数结果）

| 观测时间 | v2（S4） | v3（S5） |
| --- | --- | --- |
| 14:59 | 已知 | 已知 |
| 15:00 | 已知 | 已知（边界含） |
| 15:01 / 15:05 / 22:00 | 已知（错误） | `POST_CLOSE_UNKNOWN` / 后得 |
| D+1 09:00 | `LATER` | `LATER` |
| UTC 06:59 | 已知 | 已知（+08:00 换算） |
| UTC 07:01 | 已知（错误） | `POST_CLOSE_UNKNOWN` |
| 日线（无 observed_at） | 已知（错误） | `END_OF_DAY_FACT` / 后得 |

机器可读：`reports/s5_pit/protocol.md`、`reports/s5_pit/pit_cases.json`。

### 2.3 测试

- `tests/test_s5_pit_times.py`：31 条（14:59/15:00/15:01/15:05/22:00/D+1、UTC/UTC+8、naive、缺时间、来源未就绪、published_at 晚于 cutoff、池三源独立、冲突、日终事实、D+1 不回流、缓存键版本/cutoff）；
- `tests/test_stock_research_context.py`：22 条；
- 前端 `StockResearchEvidence.test.tsx`：5 条（含「收盘后才取到」标签、日终事实区、截止时刻）。

---

## 3. B — 数据质量 v2（`0d77b69`）

- `data-quality-v2`；`status`（旧字段）与 `source_status` / `combined_view_status` 分离；
- **竞价双维度**：`auction_result_status`（0926 终值）与 `post_open_status`（0931 承接）；0926 有效 + 0931 缺失 → 整体 `PARTIAL`、`completion_proof=false`；0926 `partial` → `INCOMPLETE`；9:31 前 → `PENDING`；全部失败 → `UNVERIFIABLE`；有行但无关键帧日志 → 不 `VERIFIED`；
- **三池拆分**：`pool_up/pool_broken/pool_down` 各自状态、行数、完成证明；up 成功但 broken/down 失败或缺日志 → `combined_view_status` 不 `VERIFIED`；最新尝试失败且旧快照仍在 → `PARTIAL` + `previous_snapshot_available` / `current_attempt_failed`；
- **`_safe_source`**：查询异常 → `SOURCE_ERROR` + `DB_SCHEMA_UNAVAILABLE` / `DB_QUERY_FAILED`，不伪装 `NOT_COLLECTED`，不泄露 SQL/路径/连接串；单来源异常不影响其他来源；
- 对账器 `scripts/check_s5_quality_consistency.py`：真实只读库 15 个交易日、60 项检查、**0 unexpected**、1216 条 SQL；报告 `reports/s5_quality/contract_matrix.json`、`disagreement_report.md`；
- 契约测试 `tests/test_s5_quality_contract.py`：30 条；原 `tests/test_data_quality.py` 11 条保持通过；
- 过程中发现并修复一次口径冲突：情绪次日 `VERIFIED` vs 全市场日线 `PARTIAL`，最终判据改为「日线 ∈ {VERIFIED, PARTIAL} 且抽样 `missing_count==0`」，并加强而非放宽。

---

## 4. C — 盘后主线研究工作台（`3a5f2e6` + `8f7acc6`）

- 路由：`/research/workbench`（`RequireAuth`），导航「选股 → 研究工作台」；
- API：`GET /api/research/workbench`，参数 `date / sector_codes / mode / market / view / limit / offset / stock`，全部有上限与白名单；
- 聚合：`sentiment_next_day`、`limit_funnel`、`theme_intersection` + `sector_resonance`、`data_quality`、按需 `stock_research_context`（仅显式传 `stock`，无 N+1）；
- 页面：观察日/数据截至/质量版本置顶；情绪、漏斗、共振、D+1、数据质量、逐券表；URL 保留 `date/sector_codes/mode/market/offset`；快速日期切换用请求序号防旧响应；`StockLink` 原导航不变，`QuickResearch`「据」直达 evidence Tab；
- 真实历史只读样例：`reports/s5_workbench/sample_2026-09-24.json`（3 板块 OR，3564 行，50 SQL，冷启动 887 ms / 9.1 MB）；
- 真实浏览器：**39/39 通过**，16 张截图，覆盖 390/768/1280/1920/3840 五种视口；`reports/s5_browser/browser_results.json`、`browser_run.md`；
- 测试：后端 `tests/test_post_market_workbench.py` 24 条 + 前端 `PostMarketWorkbench.test.tsx` 10 条 = 34 个场景；
- 未实现可选的 JSON/CSV 导出（任务书写「可选」），因此无 CSV 公式注入面。

---

## 5. D — 离线闸门与工程门禁（`428c26e`）

### 5.1 网络栈清单

`reports/s5_security/network_inventory.md`：枚举 `requests / urllib3 / akshare / urllib / http.client / httpx / aiohttp / WebSocket / 裸 socket / subprocess / 原生库 / 本地回环`，标明每个来源入口的 `ensure_external_fetch` 覆盖与未覆盖项。

### 5.2 三层防护

1. 来源入口 `ensure_external_fetch`（新增 `eastmoney.fetch_daily` 闸门）；
2. `requests.Session.request` + `socket.create_connection` + `socket.socket.connect`（回环放行）；
3. 进程启动锁定：首次读到 false 即 `locked_offline=true`，运行中改回 true 也不放行。

### 5.3 实测

- 启动日志（`reports/s5_security/backend_start.txt`）：
  `EXTERNAL_DATA_FETCH_ENABLED=False（offline_locked=True）· SCHEDULER_ENABLED=False · WARMUP_PATTERN_CACHE=False · FUPAN_SCHEMA_MODE=verify`；
- 浏览器验收后 `/api/quality/egress`：`attempts=2, blocked=2, network_attempts=0, network_blocked=0, os_network_attempts=0, os_network_blocked=0, installed=true, socket_guarded=true, mode=offline_locked`（两个被来源入口提前拦截，未走到 socket）；
- `tests/test_s5_offline_gates.py` 18 条：urllib / raw socket 被 socket 层拦截、httpx 后端依赖 socket、回环放行、锁定不可逆、`stats()` 不含域名/token、503 `offline=true`；
- `tests/test_external_guard.py` 9 条保持通过；
- **OS 级隔离未实测** → `OS_EGRESS_DENY_NOT_VERIFIED`；`lark-cli` 子进程不在进程内 socket 闸门覆盖范围。

### 5.4 发布前只读门禁

`reports/s5_security/release_gate.md`：schema verify 通过（临时库）、生产库未打开、未自动 migrate、新增路由鉴权/参数上限/日志脱敏/SQL 预算/连接释放审查、回滚候选与前后端兼容风险；**未执行 merge / deploy / revert**。

---

## 6. 测试与门禁（真实执行）

| 项 | 结果 |
| --- | --- |
| `compileall -q backend scripts` | 退出码 0 |
| `ruff check backend scripts tests` | All checks passed（0） |
| `pytest -q`（最终） | **1553 passed / 4 skipped / 0 failed**，412.43 s |
| `npm test`（`--maxWorkers=1`） | **15 files / 151 tests passed** |
| `npm run lint` | 0 warnings / 0 errors |
| `npm run build` | 成功（tsc + vite） |
| Chrome 浏览器 | **39/39 通过**，0 console error |

失败与复跑记录：

- 首次全量 pytest 出现 1 个失败：`tests/test_forward_track_c0.py::test_evaluate_reports_pending_when_not_matured`，`OSError [Errno 22]` 写临时 `.replay_signals_*.tmp-*`；单独复跑通过；最终全量复跑 **0 failed**（判定为 Windows 临时文件/杀软竞争，非 S5 代码）；
- 前端默认并行 vitest 在 5 个 worker 上出现 Node OOM（环境内存压力），改用 `--maxWorkers=1` 后 151/151 通过；未删除或跳过任何测试。

---

## 7. 性能（`reports/s5_performance/*`）

- 数据库：`b1_research.db`，只读 `mode=ro&immutable=1`；观察日 2026-09-24；
- 10 板块 OR：**50 SQL / 352.06 ms / 11.3 MB / 21.4 KB JSON**；
- 1/2/3/10 板块 SQL 均为 50（与板块数无关）；
- 质量 overview 单日：18 SQL / 51.56 ms；timeline(limit_pool, 250 日)：4 SQL / 38.26 ms；
- 情绪次日 250 日：255 SQL / 949.01 ms；research-context 单股：14 SQL / 26.33 ms。

目标对照：**耗时 <500 ms 达标；SQL ≤25 未达标（50）**。瓶颈拆分与不重构理由写在 `benchmark.md`，未造数。

---

## 8. 数据、冻结与只读

### 8.1 C0 五 blob（与 S4 一致）

`1408f9e3…fdf2a`、`8064d670…fab5f`、`6ff3eb08…9abe`、`2f8979d2…2ca0`、`32b6f2ff…e80f`。

### 8.2 冻结信号（与 S4 一致）

`1F80D1B6…7B37`（jsonl）、`44301EB5…DD50`（meta.json）。

### 8.3 三库

| 库 | 大小 | mtime | integrity |
| --- | --- | --- | --- |
| `fupan.db` | 513,519,616 B | 2026-10-31 22:26:44 | ok |
| `b1_research.db` | 1,441,378,304 B | 2026-11-09 12:23:12 | ok |
| `forward_research.db` | 67,055,616 B | 2026-11-11 17:17:58 | ok |

S5 全程只读 / 临时副本；未写生产库、未重生成冻结文件。

---

## 9. 权限边界

- 未合并 master、未部署、未重启线上、未改调度、未改 C0 / 冻结文件；
- 未发起真实腾讯 / iFinD / Tushare / 开盘红 / 东财请求；
- 未写生产数据库；所有动态验收使用 `%TEMP%\s5_e2e\s5.db` 合成库；
- 浏览器验收后端仅监听 `127.0.0.1:8014`。

---

## 10. 已知限制（继续保留）

- `THEME_REAL_DATA_ACCEPTANCE_PENDING`：`sector_member` 无成员完成证明；
- `REAL_AUCTION_DATA_NOT_YET_VERIFIED`：真实库 `auction_snapshot` 仍 0 行；
- `PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、`P13_RELEASE_NO_GO`：无生产发布权限；
- `HISTORICAL_DATA_GAP_UNDER_REVIEW`：真实库板块成员覆盖不全；
- `OS_EGRESS_DENY_NOT_VERIFIED`：未做容器/防火墙/网络命名空间实测；
- D+1 统计只作复盘，不进入 C0 或任何正式信号。

---

## 11. 状态词

```text
S5_INTEGRATED_CODE_READY_FOR_INDEPENDENT_REVIEW
S5_PIT_CUTOFF_CONSISTENT
S5_QUALITY_CONTRACT_ALIGNED
S5_POST_MARKET_WORKBENCH_READY
S5_OFFLINE_GATES_REPORTED
OS_EGRESS_DENY_NOT_VERIFIED

THEME_REAL_DATA_ACCEPTANCE_PENDING
REAL_AUCTION_DATA_NOT_YET_VERIFIED
PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED
P13_RELEASE_NO_GO
HISTORICAL_DATA_GAP_UNDER_REVIEW
```

**本批次已完成、提交、推送。停止，等待集中独立审查；不开始 S6，不做生产部署或真实竞价采集。**
