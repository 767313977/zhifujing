# S6 验收报告（生产灰度前安全加固与发布准备）

- 分支：`release/s6-canary-hardening`
- 基线：`104dfdf489150d97446be3af0fa4a4de14382bbd`（含任务书基线 `3060e82`；
  `3060e82..104dfdf` 为 S5 第四轮 5 个提交：TLS write/per-socket nested、
  TLS 集成证据、sentiment fail-closed、retry generation、docs）
- 生成时间：2026-11-15（Asia/Shanghai）
- **最终状态：**
  - `S6_IMPLEMENTATION_PARTIAL`
  - `OS_EGRESS_DENY_NOT_VERIFIED`
  - `S6_CANARY_BLOCKED`
  - `PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`
- **未达到** `S6_CANARY_READY_PENDING_EXPLICIT_PRODUCTION_APPROVAL`

## 1. Git 与工作区

| 项 | 值 |
| --- | --- |
| 分支 | `release/s6-canary-hardening`（自 `104dfdf` 创建） |
| 基线祖先校验 | `git merge-base --is-ancestor 3060e82 HEAD` → exit 0 |
| 工作区 | 提交完成后 clean |
| 远端 | 本轮结束推送 `origin/release/s6-canary-hardening`；未合并 master |
| 禁止事项 | 未部署生产、未写生产库、未启动 scheduler、未真实采集 |

## 2. 原子提交清单

| 顺序 | SHA | 内容 |
| --- | --- | --- |
| S6-00 | `c9000da` | Gate 0 基线、保护资产指纹、依赖/运行环境清单、指纹脚本 |
| S6-A | `6fc4611` | 统一出站策略、DNS/代理/写路径闸门、安全矩阵与报告、OS 脚本 |
| S6-B | `b8f9905` | readiness 契约、数据库只读实测、配置矩阵、API 安全审查 |
| S6-C | `e7e710a` | 质量状态契约、7 条 NOT_COMPARABLE 登记、空时间线修复 |
| S6-D | `f7a2151` | SQL waterfall、before/after 基准、60/60 并发回归、稳定性矩阵 |
| S6-E | `b044851` | 本地隔离 Staging 部署/重启/回滚演练、浏览器验收 |
| S6-F | `0f4a050` | 灰度审批包（10 份材料） |

## 3. A–F 工作包结果

### A. 出站网络安全边界

- 完成：全局出口盘点（17 个节点）、统一 allowlist（默认拒绝）、DNS 入口闸门、
  代理过滤、socket/TLS 写路径、指标口径、readiness。
- 应用层测试：35 条 S6 用例 + 既有 91 条通过。
- **OS 级：未验证**。只交付 `deploy/s6/*.nft/.sh`，未在开发机或生产执行。
- 关键发现并修复：系统代理 `127.0.0.1:7890` 可让未授权请求绕过应用层直连判定。

### B. 运行配置、权限与数据隔离

- 完成：环境配置矩阵、health/ready 分离、启动顺序证明、数据库只读语义实测、
  API/认证审查。
- **G7 FAIL**：应用引擎仍是读写 `sqlite:///path`；用户/会话/日志与研究表同库，
  灰度需要的「只读研究库 + 独立用户库」未实现。

### C. 数据质量与灰度范围

- 7 条 `NOT_COMPARABLE` **原样保留**（2 条缺日线行且无交易资格证据、3 条上游池
  依赖未解决、2 条 D 日情绪指标缺失）；
- v4 对账：`total=76 / passed=65 / explained=4 / not_comparable=7 / unexpected=0`，
  **exit=2** 保留；
- 功能可用矩阵、历史缺口登记、质量状态契约完成；
- 修复空交易日历下 timeline 400（浏览器验收发现）。

### D. 性能、并发与稳定性

- 10 板块：**53 SQL**，P50 135.94–165.28 ms；`PERF_SQL_BUDGET_NOT_MET` 保留；
- 重复查询合计约 4 ms，未做低价值/高风险重构；
- 并发压力 **60/60** 通过（30 干净 + 30 CPU 负载），未放宽断言；
- 稳定性矩阵中标出 DB 锁、soak、磁盘、备份、故障注入为 `NOT_VERIFIED`。

### E. Staging 部署与验收

- 独立目录/端口/临时库；`EXTERNAL_DATA_FETCH_ENABLED=false`、
  `SCHEDULER_ENABLED=false`、`WARMUP_PATTERN_CACHE=false`、`FUPAN_SCHEMA_MODE=verify`；
- smoke：health 200、ready **503**、未登录 401、注册/登录 200、质量接口 200；
- 重启：通过；回滚到 `HEAD`：health 200、旧版无 `/api/ready`（404）、再切回新版通过；
- 浏览器：两轮真实浏览器验收 PASS（5 个页面、console 0）；登录页三视口截图已存。

### F. 灰度材料

`reports/s6/release/` 下 10 份材料齐全，`approval-request.md` 明确列出阻塞项与
需用户批准的最小动作。

## 4. OS 级 Egress 的真实证据

**没有。** 本轮所有网络证据都是应用层（Python 进程内 wrapper）。原因：

1. 本机是 Windows 11 开发机，无 Linux netns/容器；
2. 任务书禁止在日常开发机安装/修改全局防火墙；
3. 生产主机与生产网络不可访问。

因此 G3/G4 = `NOT_VERIFIED`，不得声称 OS 隔离通过。

## 5. 覆盖矩阵（DNS/IPv4/IPv6/TCP/UDP/子进程/原生库）

| 路径 | 应用层 | OS 层 |
| --- | --- | --- |
| requests / urllib / urllib3 | PASS | NOT_VERIFIED |
| httpx / aiohttp | 未安装；安装后经 socket（aiohttp asyncio resolver 未测） | NOT_VERIFIED |
| TCP IPv4 / IPv6 | PASS（单元） | NOT_VERIFIED |
| UDP | PASS（应用层拒绝） | NOT_VERIFIED |
| DNS（getaddrinfo） | PASS（应用层拒绝非授权） | NOT_VERIFIED |
| 子进程 / 原生库 / curl | **无法覆盖**（E16 证明子进程不受 wrapper 约束） | NOT_VERIFIED |
| 代理绕过 | PASS（应用层过滤系统/环境代理） | NOT_VERIFIED |
| 重定向 | PASS（真实本地 302） | NOT_VERIFIED |

## 6. 数据库只读验证与写入路径审计

- 临时副本只读实测：INSERT/UPDATE/DELETE/CREATE/DROP/PRAGMA/VACUUM 全部
  `attempt to write a readonly database`；可写对照全部成功；`integrity_check=ok`；
  源库 SHA256 前后一致。
- 写入路径：`auth / journal / positions / watchlist / plans / analysis / admin`
  均写同一库；`jobs/*` 由调度器触发（灰度关闭）。
- 生产库：本轮**未连接、未写入、未迁移**。

## 7. 7 条 NOT_COMPARABLE 保留情况

全部保留，未改成 `explained`，未从分母删除，未推断成停牌，未修改真实库数据。
逐条证据见 `reports/s6/data-quality/not-comparable-register.md`。

## 8. 后端、前端、浏览器测试结果

| 项 | 结果 |
| --- | --- |
| `pytest -q` | **1696 passed / 4 skipped**（347.10 s） |
| `ruff check .` | All checks passed |
| `compileall backend scripts tests` | exit 0 |
| `npm test -- --run` | **153 passed / 15 files** |
| `npm run lint` | 0 warnings / 0 errors |
| `npm run build` | 成功（dist 已生成） |
| 浏览器（Staging） | 2 轮 PASS；5 页 console 0；登录页 3 视口截图 + SHA256 |

## 9. Staging 部署、重启、回滚

见 `reports/s6/staging/deployment-record.md`、`rollback-drill.md`、
`smoke-test-results.json`。回滚为代码快照 + 配置 + 数据库副本级，不是生产发布系统回滚。

## 10. 保护资产前后对比

| 项 | 结果 |
| --- | --- |
| C0 五 blob | 完全一致 |
| 两份冻结信号 SHA256 | 完全一致 |
| `fupan.db` / `history.db` / `b1_research.db` / `forward_research.db` | size/mtime/SHA256 一致，integrity 全部 `ok` |
| Git 状态（C0 与冻结文件） | 无修改、无 untracked |

## 11. 未关闭风险与被禁用功能

**阻塞风险**

1. OS 级出站默认拒绝未验证（G3/G4）；
2. 数据库只读隔离未落地（G7）；
3. 子进程（`lark-cli`）出口应用层不可控。

**披露项**

- 53 SQL > 25；
- 7 条 `NOT_COMPARABLE`；
- 竞价真实数据未验收；
- 题材真实覆盖未验收；
- 历史数据缺口；
- 依赖漏洞扫描未执行；
- 系统代理/浏览器截图导出等环境限制。

**灰度禁用**

- 全部外部取数、定时采集、竞价采集、飞书推送（webhook 与 lark-cli）、
  管理员写操作、用户写入类功能（只读库未拆分前）。

## 12. 是否满足 Canary Ready

**否。** 不满足 `S6_CANARY_READY_PENDING_EXPLICIT_PRODUCTION_APPROVAL`。

## 13. 阻塞 Gate 与下一步最小动作

| 阻塞 Gate | 最小动作 |
| --- | --- |
| G3/G4 | 在隔离 Linux Staging 执行 `apply-egress-deny.sh` + `verify-egress.sh`，全部 block 用例 PASS，并保存 JSON |
| G7 | 引擎支持只读 URL + 拆分独立用户库；再用 `/api/ready` 证明 `DATABASE_READ_ONLY_VERIFIED=PASS` |
| 子进程 | OS 策略阻断或灰度禁用（已禁用） |

在上述动作完成前，只能停留在 `S6_IMPLEMENTATION_PARTIAL` / `S6_CANARY_BLOCKED`。
