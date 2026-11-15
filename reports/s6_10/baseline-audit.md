# S6.10 基线审计（只读）

- 审计时间：2026-11-15（Asia/Shanghai）
- 执行人：自动化审计（本地工作区）
- 结论：**基线一致，无未知修改、无并发写入冲突**；但**生产主机本轮无法只读访问**，
  生产实况一律标记 `NOT_VERIFIED`，不以本机/Staging 配置代替。

## 1. Git 基线

| 项 | 值 |
| --- | --- |
| 分支 | `work/s6-8-s6-9-gate-closure` |
| 当前 HEAD | `6b8a68851b35e68e1924f1705c8c083ac4db77d1` |
| upstream | `origin/work/s6-8-s6-9-gate-closure` |
| 与 upstream 差异 | `0 0`（无 ahead / behind） |
| 任务书给出的“最近报告 HEAD” | `6b8a6885…` —— 与审计时 HEAD **一致** |

未执行 `reset` / `clean` / `stash` / `amend` / `force push`。

## 2. 工作区修改（全部为本轮 S6.10 自带）

| 文件 | 性质 | 说明 |
| --- | --- | --- |
| `backend/app/external_guard.py` | 修改 | loopback 端口收敛 + 公网 IP 断言 + 策略判定 |
| `deploy/s6/egress-policy.json` | 修改 | `loopback.allow_any_port=false`、`ports=[5000]` |
| `backend/app/services/stock_phase.py` | 修改 | 拆分只读模式下改用 `research_session_scope`（修复 `/api/stock/{code}` 500） |
| `backend/app/config.py` | 修改 | 新增 `auction_collect_enabled=False` |
| `backend/app/jobs/scheduler.py` | 修改 | 竞价任务按开关注册 |
| `deploy/install.sh` | 修改 | systemd 增加 `AUCTION_COLLECT_ENABLED=false` |
| `tests/test_s6_app_egress_ssrf.py` / `test_s6_egress_boundary.py` / `test_s5_offline_gates.py` | 修改 | loopback 端口策略、TLS sendfile 竞态、SSRF 新用例 |
| `tests/test_s6_10_go_live_config.py` / `test_s6_10_stock_phase_readonly.py` | 新增 | 上线配置与只读会话测试 |
| `scripts/s6_10_core_business_acceptance.py` | 新增 | 真实研究库只读核心业务验收 |
| `scripts/s6_10_browser_history.py`、`scripts/s6_8_cdp_e2e.mjs` | 新增/修改 | 深层链接、刷新、前进后退浏览器验收 |
| `reports/s6_10/` | 新增 | 本阶段证据 |
| `reports/s6/s62/migration-report.json` | 修改（**测试产物**） | `tests/test_s6_migration*` 每次运行都会重写该文件（临时路径、时间戳、摘要），非业务改动 |

**未知修改：无。** 上表每一处都能对应到本轮任务或既有测试的自动产物。

## 3. 保护资产（G0/C0）

`reports/s6_10/protected-assets-before.json`（`scripts/s6_gate0_fingerprint.py --stage before`，exit 0）：

- `all_protected_assets_present = true`；
- `missing_assets = []`、`integrity_failed = []`；
- C0 五脚本 git blob 与 worktree SHA256 全部记录，且与本轮开始前一致：

| 文件 | worktree SHA256（前 16 位） |
| --- | --- |
| `backend/app/services/factor_score.py` | `7063bb9dc0db7f88` |
| `scripts/forward_collect_tushare.py` | `3aec889004a7f7d3` |
| `scripts/forward_store.py` | `f4b0a8ea4de5eed1` |
| `scripts/forward_track_c0.py` | `9d20a569bacbb2c6` |
| `scripts/forward_universe.py` | `fb07ce84c79f7faa` |

- 冻结信号：`backend/data/forward_c0/signals_2026-11-11.jsonl` =
  `1f80d1b6…7b37`，`.meta.json` = `44301eb5…dd50`（与 S6.8/S6.9 一致）。

## 4. 数据库（四库 + 保护副本）

`reports/s6_10/database-integrity-before.txt`（`mode=ro&immutable=1`，`PRAGMA integrity_check`）：

| 数据库 | 用途 | integrity | 大小 | 主文件 mtime |
| --- | --- | --- | --- | --- |
| `backend/data/fupan.db` | 旧单库（生产样式） | ok | 513,519,616 | 2026-10-31 22:26:44 |
| `backend/data/history.db` | 历史库 | ok | 869,801,984 | 2026-10-20 14:14:38 |
| `backend/data/b1_research.db` | 研究库（只读） | ok | 1,441,378,304 | 2026-11-09 12:23:12 |
| `backend/data/forward_research.db` | 前瞻研究库 | ok | 67,055,616 | 2026-11-11 17:17:58 |

- 四库 WAL 大小均为 0；`*-shm` 存在属正常（连接打开时创建）；
- `b1_research.db` / `fupan.db` / `history.db` 超过 200 MB，按任务书只记录大小/mtime，
  不计算整文件 SHA256；核心业务验收脚本另行计算 `b1_research.db` 全文件 SHA256 作前后对照；
- `b0_trial*.db`、`b2_final_snapshot.db`、`p092_*`、`p10_*` 为历史验证副本，本轮未读写。

## 5. 生产部署目录与调度配置（仓库侧只读核对）

| 项 | 值 | 证据 |
| --- | --- | --- |
| 生产目录 | `/opt/fupan`（`APP_DIR`） | `deploy/install.sh` |
| 生产服务 | systemd `fupan.service`，`127.0.0.1:8000` | `deploy/install.sh` |
| 生产调度 | `Environment=SCHEDULER_ENABLED=true` | `deploy/install.sh` |
| 真实竞价采集 | `Environment=AUCTION_COLLECT_ENABLED=false`（本轮新增） | `deploy/install.sh` |
| 生产 SSL 校验 | `IFIND_CA_FILE=/etc/ssl/certs/ca-certificates.crt` | `deploy/install.sh` |
| 生产 CORS | `ENABLE_DEV_CORS=false` | `deploy/install.sh` |
| 本机开发 `.env` | `HOST=0.0.0.0`、`SCHEDULER_ENABLED=false` | 本机 `.env`（仅键名与非敏感值） |

⚠️ 以上均为**仓库内声明**；生产主机上的实际 `systemctl cat fupan`、`/opt/fupan/.env`、
nginx 站点配置**本轮未采集到**，见 `production-config-audit.md` 的 `NOT_VERIFIED` 项。

## 6. 并发与环境

- 审计时无 uvicorn / FastAPI 服务进程在跑（仅本轮的 pytest 进程）；
- 未发现锁文件、备份任务或其它写库进程；
- 宿主机为 Windows（`win32`），Python 3.13.13，Node v24.19.0；
- 本轮全部生产相关动作**未执行**：无部署、无 merge、无启用调度、无启用真实采集、
  未修改 `S6_CANARY_READY`。

## 7. 现有 S6.8/S6.9 报告

`reports/s6_8_9/` 完整存在（rebalance / browser / staging / soak / security / performance /
data / vm / egress / logs + `S6-8-9-balanced-acceptance-report.md` + `SHA256SUMS`），本轮直接引用，
不覆盖、不删除。

## 8. 基线判定

```text
baseline_consistent = YES
unknown_modifications = NONE
protected_assets_changed = NO
concurrent_writer = NONE
production_host_accessible = NO (NOT_VERIFIED)
```

允许进入 S6.10 后续审计与收口。
