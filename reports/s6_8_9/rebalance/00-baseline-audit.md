# S6.8/S6.9 平衡方案：基线审计（只读）

- 时间：2026-11-15（Asia/Shanghai）
- 执行模式：C — 平衡方案
- 审计方式：只读；未启动/停止 VM、未执行 Guest 相关命令、未运行防火墙脚本
- 结论：**基线一致，可进入重分类与 Release Critical 收尾**

## 1. Git

| 项 | 值 |
| --- | --- |
| 分支 | `work/s6-8-s6-9-gate-closure` |
| HEAD | `fa2a3ac801bbc032b605e1ad86960da03d433490` |
| upstream | `origin/work/s6-8-s6-9-gate-closure` |
| ahead / behind | 0 / 0 |
| 工作区 | clean（`git status --short` 无输出） |
| `git diff --check` | exit 0（仅 CRLF 提示） |
| `fa2a3ac` 是否祖先 | 是（当前 HEAD 即该提交） |

未执行 reset / clean / stash / amend / force push。未发现并发写入或未知变更。

## 2. 保护资产 G0（before）

- 脚本：`scripts/s6_gate0_fingerprint.py --stage before --out-dir reports/s6_8_9/rebalance`
- 结果：`protected_assets_present=True`、`missing=[]`、`integrity_failed=[]`
- JSON SHA256：`3f7c5a0447e13a16c464b403368d01ad015a8b3a014dbb0ddcbd1c5e7078ec17`
- 明细：`rebalance/protected-assets-before.json`、`database-integrity-before.txt`

## 3. 当前 VM（只读）

| 项 | 值 |
| --- | --- |
| 名称 | `s6-egress-vm` |
| 状态 | **Running**（uptime ≈ 14 分钟） |
| 规格 | Gen2 / 4 vCPU / 6 GB / 动态内存关闭 |
| 磁盘 | `D:\s6-vm\s6-egress-vm-os.vhdx`，Dynamic 64 GB |
| vSwitch | `s6-egress`（Internal） |
| NAT | `s6-egress-nat` → 172.31.240.0/24 |
| 快照 | 0 |
| Guest OS | 未安装 |
| Guest Bootstrap / apply / N01–N24 | 未执行 |

按任务书：保留 VM/VHDX/vSwitch/NAT/ISO 与全部证据，标记 `DEFERRED_SECURITY_LAB`；
不删除、不强停。建议用户确认后正常 `Stop-VM` 以释放 6 GB 内存。

## 4. 已有成果清单

| 类别 | 状态 | 证据 |
| --- | --- | --- |
| S6.7 登录白屏修复 | PASS | `reports/s6_7_login/` |
| E13 双 Context | PASS | `reports/s6_8_9/browser/e13_context_isolation.json` |
| E15 Chunk 404 | PASS | `browser/e15_chunk_404.json` |
| E16 渲染异常 | PASS | `browser/e16_render_error.json` |
| 三视口 | PASS | `browser/viewports.json` + 6 张截图 |
| OLD/NEW 真回滚 | PASS | `staging/version_switch.json`、`manifest-old/new.json` |
| G7 | PASS | 185 路由、readiness 9/9、迁移幂等/恢复 |
| 后端回归 | 1735 passed / 4 skipped | `logs/backend_test.log` |
| 前端回归 | 168 passed / 20 files | `logs/frontend_test.log` |
| 依赖/敏感扫描 | 0 / 0 / 0 | `logs/security_scan.log`、`sensitive_scan.log` |
| SQL | 53 > 25 | `performance/sql_profile.json` |
| 7 NOT_COMPARABLE | 保留 | `data/not_comparable_recheck.md` |

## 5. 与旧基线的差异

- 旧报告中的 `S61_VM_UNAVAILABLE` 已被“Hyper-V 管理面 PASS + VM 创建成功”取代；
  但 **Guest OS 仍未安装**，OS Egress 仍为 `NOT_VERIFIED`。
- 本轮不再把 VM 安装/N01–N24 作为 S6.8 阻断项，改为 `DEFERRED` 并登记风险。
- 旧 S6.8 报告中的 C 包描述以 `vm/vm_create_report.md` 与本文件为准。

## 6. Staging 环境口径

- 浏览器 E2E：`127.0.0.1:8021`，拆分库（Research 只读 + User 可写），
  `EXTERNAL_DATA_FETCH_ENABLED=false`、`SCHEDULER_ENABLED=false`；
- 版本回滚：`127.0.0.1:8012`，独立临时库，外部取数/调度关闭；
- 未连接生产库或真实用户数据。

## 7. 结论

```text
baseline = CONSISTENT
worktree = clean
HEAD == upstream
G0 protected assets = present, no integrity failures
VM = DEFERRED_SECURITY_LAB (Running, Guest not installed)
```

可以按平衡方案继续：先完成门禁重分类/风险登记，再补齐 Release Critical 缺项。
