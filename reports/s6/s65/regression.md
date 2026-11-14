# S6.5 全量回归

| 门禁 | 命令 | 退出码 | 结果 |
| --- | --- | --- | --- |
| 后端测试 | `.venv/Scripts/python.exe -m pytest -q` | 0 | **1711 passed / 4 skipped**（358.29 s） |
| Ruff | `python -m ruff check .` | 0 | All checks passed |
| compileall | `python -m compileall -q backend` | 0 | 通过 |
| diff check | `git diff --check` | 0 | 无空白错误 |
| 前端测试 | `npm test -- --run` | 0 | **154 passed / 16 files** |
| 前端 lint | `npm run lint` | 0 | 0 warnings / 0 errors（133 files） |
| TypeScript | `npx tsc -b` | 0 | 通过 |
| 前端构建 | `npm run build` | 0 | 成功（dist 已生成） |
| 浏览器验收 | Staging 真实浏览器 | — | PASS（5 页、console 0） |
| 性能 | `benchmark_s5.py` | 0 | 53 SQL / 123.48 ms（10 板块） |
| Staging 回滚 | `s6_staging_drill.py` | 0 | 部署/重启/回滚/再部署全部 PASS |

## 新增测试

- `tests/test_s6_egress_scripts.py`：10 条（OS 脚本静态安全与 `bash -n`）
- `tests/test_s6_db_split.py`：5 条（只读、迁移幂等、缺库 fail closed）
- `frontend/src/components/__tests__/AdvanceDeclinePanel.test.tsx`：1 条（404 空态 loading 结束）

## 未执行

- 24 小时 soak（仅 2 分钟短窗，见 `reports/s6/s64/soak-report.md`）
- 崩溃/SIGKILL/磁盘满/锁争用注入（`NOT_RUN`）
- 生产备份恢复演练（`NOT_RUN`，无生产权限）
