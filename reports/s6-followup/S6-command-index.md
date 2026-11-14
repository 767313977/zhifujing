# S6 后续攻坚：命令索引

- 分支：`release/s6-canary-hardening-v3`
- 证据截止：`53b4b05`（文档提交前）
- 工作目录：仓库根 `c:\Users\76731\Documents\trae_projects\fupan`

| 阶段 | 命令 | 退出码 | 结果 / 证据 |
| --- | --- | --- | --- |
| Gate0 | `git status --short` / `git rev-parse HEAD` | 0 | clean，基线 `5db5246` → `gate0/git-baseline.json` |
| Gate0 | `python scripts/s6_gate0_fingerprint.py --stage gate0 --out-dir reports/s6-followup/gate0` | 0 | `gate0/protected-assets-before.json/.sha256` |
| Gate0 | `python scripts/s6_gate0_fingerprint.py --stage final --out-dir reports/s6-followup/gate0` | 0 | `gate0/protected-assets-final.json/.sha256` |
| Gate0 | before/final 逐项对比脚本 | 0 | `diffs=0` |
| G7 | `python scripts/s7_route_inventory.py` | 0 | `g7/route-db-ownership.csv`（185 行） |
| G7 | `python scripts/s7_migration_revalidate.py` | 0 | `g7/migration-revalidation.json`（PASS） |
| G7 | `python scripts/s7_readiness_probe.py` | 0 | `g7/readiness-proof.json`（9/9 true） |
| G7 | `pytest tests/test_s7_route_ownership.py tests/test_s7_db_negative_matrix.py -q` | 0 | 全通过 |
| G7 | `python scripts/s6_init_user_db.py`（隔离目录） | 0 | User DB 显式初始化 |
| S6.1 | `bash -n deploy/s6/*.sh` | 0 | 语法通过；规则审计 `s61/policy-audit.md` |
| S6.1 | `S6_ISOLATED_VM=1 bash deploy/s6/apply-egress-deny.sh` | **NOT_RUN** | 无隔离 Linux VM |
| S6.1 | `S6_ISOLATED_VM=1 bash deploy/s6/verify-egress.sh` | **NOT_RUN** | 同上 |
| S6.1 | `powershell -File deploy/s6/vm/prepare-hyperv-vm.ps1` | **NOT_RUN** | 本机未安装 Hyper-V |
| S6.4 | `python scripts/s7_soak_split.py --minutes 3 --faults` | 0 | `s64/soak-metrics.json`、`fault-injection.json` |
| S6.4 | 24h soak | **NOT_RUN** | `S6_STAGING_SOAK_PASSED=NOT_RUN` |
| S6.4 | `pip-audit -r backend/requirements.txt` | 0 | No known vulnerabilities |
| S6.4 | `npm audit --registry=https://registry.npmjs.org` | 0 | 0 vulnerabilities |
| S6.5 | `pytest -q` | 0 | 1735 passed / 4 skipped（363.49 s） |
| S6.5 | `ruff check backend tests scripts` | 0 | All checks passed |
| S6.5 | `python -m compileall backend` | 0 | 通过 |
| S6.5 | `npm test`（vitest run） | 0 | 154 passed / 16 files |
| S6.5 | `npm run lint` | 0 | 0 warnings / 0 errors |
| S6.5 | `npm run build` | 0 | 成功（vite 8.3.8） |
| S6.5 | `python scripts/benchmark_s5.py --db backend/data/b1_research.db --date 2026-09-24 --sectors 801572,801366,803024,801085,801033 --out reports/s6-followup/s65/benchmark.json` | 0 | 53 SQL，`PERF_SQL_BUDGET_NOT_MET` |
| S6.5 | 浏览器验收（TRAE 浏览器代理，`127.0.0.1:8021`） | **FAIL** | `s65/browser.md`：登录白屏 |
| S6.5 | `python scripts/s6_staging_drill.py --snapshot 53b4b05 --out-dir reports/s6-followup/staging` | 0 | `staging/smoke-test-results.json` |
| S6.6 | `git diff --check` | 0 | 无空白错误 |
| S6.6 | 生产灰度部署 | **NOT_RUN** | 未授权、门禁未过 |

## 不可执行项说明

- **OS egress**：`S61_VM_UNAVAILABLE`，任务书禁止宿主/WSL 应用防火墙；
- **24h soak**：本机无法保持 24h 连续环境，短窗不冒充；
- **生产备份恢复 / 灰度**：无生产权限，且强制门禁未全部 PASS。
