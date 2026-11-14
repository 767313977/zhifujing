# S6 后续攻坚：实施与灰度验收报告

- 起始 SHA：`5db5246010c884a8a980e8279e4b49cb164a3603`（clean）
- 结束 SHA（文档提交前）：`53b4b05ae2c3091eaaefe570fed7c7763025a206`
- 分支：`release/s6-canary-hardening-v3`（自 `release/s6-canary-hardening-v2` 创建，未改写历史、未 force-push）
- 工作区：`C:\Users\76731\Documents\trae_projects\fupan`；Python 3.13.13；Windows 11
- 生成时间：2026-11-15（Asia/Shanghai）
- **最终状态：`S6_CANARY_BLOCKED`**
- **合并 master：未执行；生产部署：未授权、未执行；灰度入口：未开放**

## 1. 阶段结果

| 阶段 | 目标 | 结果 | 证据 |
| --- | --- | --- | --- |
| Gate 0 | 基线 / 冻结信号 / 四库指纹 | **PASS** | `gate0/git-baseline.json`、`protected-assets-before|final.json`、`database-integrity-*.txt` |
| A（G7） | 全路由数据库归属与 Session 切换 | **PASS** | `g7/G7-db-routing-verification.md`、`route-db-ownership.csv`、`table-ownership-final.csv`、`write-path-inventory.json` |
| A（readiness） | 真实运行时数据库门禁 | **PASS** | `g7/readiness-proof.json`（9 项全 true） |
| A（负向矩阵） | 双库读写负向测试 | **PASS** | `tests/test_s7_db_negative_matrix.py`、`tests/test_s7_route_ownership.py` |
| A（迁移） | 513MB 副本迁移复核 | **PASS** | `g7/migration-revalidation.json`（幂等 + 中断恢复 + 源 SHA256 不变） |
| B（S6.1） | OS egress 与隔离 VM | **NOT_VERIFIED**（`S61_VM_UNAVAILABLE`） | `s61/verification.json`、`s61/vm-inventory.json`、`deploy/s6/vm/` |
| C（S6.4） | soak / 故障注入 / 漏洞扫描 | soak **NOT_RUN**；故障注入 **PASS**；漏洞 **PASS** | `s64/soak-report.md`、`fault-injection.json`、`vulnerability-scan.md` |
| D/E（S6.5） | 全量回归 / 浏览器 / 性能 / Staging | 自动化 **PASS**，浏览器 **FAIL**，性能 **未达预算**，Staging **PASS** | `s65/*`、`staging/*` |
| F（S6.6） | Gate Matrix / 验收 / 发布 | **BLOCKED** | `S6-gate-matrix.json` |

## 2. 关键代码改动与函数

### 2.1 数据库层

- [db.py](file:///c:/Users/76731/Documents/trae_projects/fupan/backend/app/db.py)：
  `ResearchDBReadOnlyError`、`research_writable()`、`research_session_scope()`；
  `session_scope()` 在拆分形态下拒绝写研究库；`init_db`/`migrate_schema`/`schema_report`/
  `ensure_schema` 拆分感知（研究库只建 37 张研究表，用户库缺表 fail closed，不自动建库）。
- [deps.py](file:///c:/Users/76731/Documents/trae_projects/fupan/backend/app/api/deps.py)：
  新增 `require_research_writable()`（灰度 503）；`resolve_trade_date` 改研究库。
- [readiness.py](file:///c:/Users/76731/Documents/trae_projects/fupan/backend/app/readiness.py)：
  `database_checks()` 9 项真实检查（SPLIT / 路径 / `mode=ro` / `query_only` /
  只读写探针 / 文件权限 / User DB 存在与可写）。

### 2.2 路由层

- A1 用户域：`auth`、`note` 仅 User；`watchlist`、`journal`、`positions`、`plans`、
  `risk`、`analysis`、`admin` 双 Session 显式注入。
- A2 研究域：`market`、`limit`、`themes`、`pools`、`patterns`、`valuation`、`cb`、
  `etf`、`auction`、`quality`、`funds`、`research`、`sector`、`stock`、`anomaly_map`
  改 `Depends(get_research_db)`；`sector._board_members`、`stock._stock_dde`
  只读 scope + 灰度拒绝隐式补采；`anomaly_map.build_anomaly_map` 删除内部 `SessionLocal()`。
- A3 混合 / 后台：`services/usage.py` 只读查询改研究库、记账仍写库；
  `jobs/collect_daily.sync_watchlist`、`jobs/push_brief`、`services/brief_watchlist` 双 Session。

### 2.3 测试与工具

- 新增 `tests/test_s7_route_ownership.py`（平铺 `_IncludedRouter`，AST 禁止
  `SessionLocal()` / `session_scope()` / `Depends(get_db)`）；
- 新增 `tests/test_s7_db_negative_matrix.py`（只读写入拒绝、User 可写探针无痕、
  9 项门禁健康/缺失/同路径/只读/可写负向）；
- 新增脚本：`scripts/s7_route_inventory.py`、`scripts/s7_migration_revalidate.py`、
  `scripts/s7_readiness_probe.py`、`scripts/s7_soak_split.py`、`scripts/s6_init_user_db.py`；
- 依赖补钉：`backend/requirements.txt`（`certifi>=2026.10.19`、`soupsieve>=4.0.0`）；
  `frontend/package-lock.json`（vite 8.3.8、source-map-js）。
- VM 工具：`deploy/s6/vm/prepare-hyperv-vm.ps1`（UTF-8 BOM）、`deploy/s6/vm/README.md`。

## 3. 实际命令 / 退出码 / 关键结果

| 命令 | 退出码 | 结果 |
| --- | --- | --- |
| `python scripts/s6_gate0_fingerprint.py --stage final --out-dir reports/s6-followup/gate0` | 0 | 资产齐全、`integrity_failed=[]`、与 before 逐项 diff=0 |
| `python scripts/s7_route_inventory.py` | 0 | 185 行路由 × 连接域 |
| `python scripts/s7_migration_revalidate.py` | 0 | 源 SHA256 `b65d59a6…` 前后一致；幂等/恢复 PASS |
| `python scripts/s7_readiness_probe.py` | 0 | `table_count=37`；9 项 true；DB gate PASS |
| `python scripts/s7_soak_split.py --minutes 3 --faults` | 0 | 5632 请求、0 非预期 5xx；5 项故障注入 PASS |
| `pytest -q` | 0 | **1735 passed / 4 skipped**（363.49 s） |
| `ruff check backend tests scripts` | 0 | All checks passed |
| `python -m compileall backend` | 0 | 通过 |
| `npm test`（vitest run） | 0 | **154 passed / 16 files** |
| `npm run lint`（oxlint） | 0 | 0 warnings / 0 errors（133 files） |
| `npm run build` | 0 | 成功（vite 8.3.8） |
| `pip-audit -r backend/requirements.txt` | 0 | No known vulnerabilities |
| `npm audit` | 0 | 0 / 0 / 0 / 0 / 0 |
| `python scripts/benchmark_s5.py ...` | 0 | workbench 53 SQL，中位 100.37 ms |
| `python scripts/s6_staging_drill.py --snapshot 53b4b05 --out-dir reports/s6-followup/staging` | 0 | 部署/重启/回滚/再部署 PASS |
| `bash deploy/s6/*.sh`（真实 VM） | **NOT_RUN** | 无独立 VM |
| 24h soak | **NOT_RUN** | 仅 3 分钟短窗 |

明细见 [S6-command-index.md](./S6-command-index.md)。

## 4. 测试总数与 skip

- 后端：1735 passed / 4 skipped；4 个 skip 均为既有环境相关用例（转发/外部依赖），
  **本轮未新增 skip、未放宽断言**。
- 前端：154 passed / 16 files；oxlint 0 warning / 0 error；`tsc -b` + vite build 成功。

## 5. OS 规则命中

- **无**。本轮未在任何宿主机或 WSL 安装/修改防火墙规则（任务书 §0.3 明确禁止）。
- 仅做静态校验：`deploy/s6/*.sh` 语法、规则审计（`s61/policy-audit.md`）；
  `S61_OS_EGRESS_VERIFIED = NOT_VERIFIED`。

## 6. 数据库权限证据

隔离拆分环境（`s7_readiness_probe.py`，Research `mode=ro` + User 可写）：

```text
SPLIT_ENABLED=true            RESEARCH_DB_EXISTS=true
PATHS_DISTINCT=true           RESEARCH_ENGINE_READONLY_URI=true
QUERY_ONLY_ENABLED=true       READONLY_WRITE_PROBE_BLOCKED=true
RESEARCH_FILE_PERMISSION_READONLY=true
USER_DB_EXISTS=true           USER_DB_WRITABLE=true
DATABASE_READ_ONLY_VERIFIED=PASS（9/9）
```

3 分钟拆分 soak：研究读 3072 全 200；研究写 1024 全 503；
用户写 1536 全成功；`errors_5xx=0`；两库 `integrity=ok`。

513MB 副本迁移：共享 8 表行数/PK/关键字段一致，2 张源缺表现建空表；
schema fingerprint `020e0f10…`；copy SHA256 前后一致；源库 SHA256 不变。

## 7. 浏览器验收（含 network / console）

- 环境：本地隔离拆分 Staging `http://127.0.0.1:8021`（Research 只读 + User 可写）。
- 通过：未登录跳登录页；登录接口 200；刷新后 dashboard 正常（涨停 40 家 / 最高 4 板）；
  `/limit-up`、`/sectors` 正常；`/api/ready` 语义正确（503 且 DB 门禁 PASS）；
  `/api/admin/status` 修复后 200。
- **阻断 FAIL**：登录成功后 URL 停留 `/login`、`#root` 清空、body 空文本，
  等待 9s 无变化；3 次独立复现（含一次代码实验后复测）；刷新后恢复。
  已排除后端 500、加载态、`/api/admin/status`、仅声明式跳转。
- 截图：本轮无截图文件落盘（浏览器代理抓取 DOM / network 证据，记录于
  [browser.md](./s65/browser.md)）；控制台多次报 `Debugger is not attached`，
  捕获不完整，**不能据此断言无异常**。
- 记录为 `LOGIN_SPA_TRANSITION_BLANK`，G8 = FAIL。

## 8. 性能

| 用例 | SQL | 中位耗时 |
| --- | --- | --- |
| workbench（1–5 板块） | **53** | 100.37–110.70 ms |
| quality overview | 21 | 36.53 ms |
| sentiment next-day | 255 | 287.70 ms |

`PERF_SQL_BUDGET_NOT_MET` 保留：53 > 25，不调整预算、不宣称通过；
本轮未做 N+1 重构（缺少等价性测试前不改查询结构）。

## 9. 保护资产 before / after

- C0 五脚本：git blob 与 worktree SHA256 **一致**；
- 冻结信号 `signals_2026-11-11.jsonl` / `.meta.json`：SHA256 **一致**；
- 四库（`b1_research.db` / `forward_research.db` / `fupan.db` / `history.db`）：
  size / mtime / integrity **一致**；>200MB 库按任务书只记录 size/mtime 与 integrity；
- `diff` 结果：**0**；生产库未写入、未迁移、未替换。
- 明细：[S6-protected-assets-final.json](./S6-protected-assets-final.json)。

## 10. 未关闭项

1. **G3/G4**：OS egress 未验证（`S61_VM_UNAVAILABLE`，无独立 VM）；
2. **G8**：登录后 SPA 白屏（阻断级，根因未定位）；
3. **SOAK**：24h soak 未执行；崩溃恢复、磁盘满、权限撤销、多 worker 未覆盖；
4. **S6_SECURITY_REGRESSION_PASSED**：应用层 PASS，OS 层 NOT_VERIFIED；
5. **PERF**：53 SQL > 25 保留；
6. **DATA**：7 条 NOT_COMPARABLE、题材/竞价/历史缺口保留；
7. 生产备份恢复 NOT_RUN（无生产权限）；依赖许可证未知项沿用上轮披露。

## 11. 状态与下一步

```text
S6_CANARY_READY = BLOCKED
S6_CANARY_ACTIVE = 不成立
S6_CANARY_PASS = 不成立
final_state = S6_CANARY_BLOCKED
```

按顺序的最小动作：

1. 修复 `LOGIN_SPA_TRANSITION_BLANK`，复跑浏览器验收与全量回归；
2. 提供独立 Hyper-V/KVM Linux VM，按 `deploy/s6/vm/README.md` 执行 N01–N24 并回填
   `s61/verification.json`；
3. 在隔离 Staging 完成 24h soak、崩溃/磁盘满/权限撤销/并发矩阵；
4. 以上全部 PASS 后再申请最小内部灰度；本轮不合并、不部署。

**证据 SHA256 见 [SHA256SUMS](./SHA256SUMS)。**
