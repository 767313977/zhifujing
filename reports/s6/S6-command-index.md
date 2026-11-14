# S6 命令索引

| 阶段 | 命令 | 退出码 | 结果文件 |
| --- | --- | --- | --- |
| Gate0 | `git rev-parse HEAD` / `git status --short` | 0 | `gate0/git-baseline.json` |
| Gate0 | `python scripts/s6_gate0_fingerprint.py --stage gate0` | 0 | `gate0/protected-assets-gate0.json` |
| S6.1 | `python -m pytest tests/test_s6_egress_scripts.py -q` | 0 | 10 passed |
| S6.1 | `bash -n deploy/s6/*.sh`（Git Bash） | 0 | 见单测 |
| S6.2 | `python -m pytest tests/test_s6_db_split.py -q` | 0 | 5 passed |
| S6.2 | `python scripts/s6_migrate_user_db.py ... --dry-run` | 0 | `s62/migration-dry-run.json` |
| S6.2 | `python scripts/s6_migrate_user_db.py ... --apply` ×2 | 0 | `s62/migration-report*.json` |
| S6.4 | `pip-audit -r backend/requirements.txt` | 0 | `s64/pip-audit.json` |
| S6.4 | `npm audit --omit=dev --registry=https://registry.npmjs.org` | 0 | `s64/npm-audit.json` |
| S6.4 | `python scripts/s6_soak.py --minutes 2` | 0 | `s64/soak-metrics.json` |
| S6.5 | `python -m pytest -q` | 0 | 1711 passed / 4 skipped |
| S6.5 | `python -m ruff check .` | 0 | All checks passed |
| S6.5 | `python -m compileall -q backend` | 0 | 通过 |
| S6.5 | `npm test -- --run` | 0 | 154 passed |
| S6.5 | `npm run lint` | 0 | 0/0 |
| S6.5 | `npm run build` | 0 | 成功 |
| S6.5 | `python scripts/benchmark_s5.py ...` | 0 | `s65/benchmark.json` |
| S6.5 | `python scripts/s6_staging_drill.py --snapshot <sha>` | 0 | `staging/*` |
| S6.1 | `S6_ISOLATED_VM=1 bash deploy/s6/apply-egress-deny.sh` | **NOT_RUN** | 无隔离 Linux VM |
| S6.1 | `S6_ISOLATED_VM=1 bash deploy/s6/verify-egress.sh` | **NOT_RUN** | 同上 |
| S6.4 | 24h soak | **NOT_RUN** | 仅 2 分钟短窗 |
| S6.6 | 生产灰度部署 | **NOT_RUN** | 未授权/门禁未过 |
