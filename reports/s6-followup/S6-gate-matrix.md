# S6 最终 Gate Matrix（本轮）

- 分支：`release/s6-canary-hardening-v3`
- 基线：`5db5246`（clean）
- 证据截止：`53b4b05`（最终文档提交前）
- 生成时间：2026-11-15（Asia/Shanghai）
- **最终状态：`S6_CANARY_BLOCKED`**
- 合并 master：未执行；生产部署：未授权、未执行

| Gate | 条件 | 状态 | 阻断 | 依据 |
| --- | --- | --- | --- | --- |
| G0 | clean Git、资产基线清楚 | **PASS** | 是 | `gate0/*`；before/final 逐项 diff=0 |
| G3 | OS 级默认拒绝 | **NOT_VERIFIED** | 是 | `s61/verification.json`（S61_VM_UNAVAILABLE） |
| G4 | DNS/IPv4/IPv6/TCP/UDP/代理/TLS/子进程/原生 | **NOT_VERIFIED** | 是 | N01–N24 全 NOT_RUN |
| G7 | Research 全路径只读、User 独立可写 | **PASS** | 是 | `g7/*`；185 路由矩阵、9 项 readiness、迁移复核 |
| G8 | 后端/前端/浏览器/readiness 全量回归 | **FAIL** | 是 | `s65/browser.md`：登录后 SPA 白屏 |
| G9 | Staging 部署、备份、回滚实测 | **PASS** | 是 | `staging/*`；部署/重启/回滚/再部署 PASS |
| G10 | 无阻断级漏洞、无数据损坏 | **PASS** | 是 | `s64/vulnerability-scan.md`；四库 integrity=ok |
| SOAK | 连续 24h Staging soak | **NOT_RUN** | 是 | 仅 3 分钟短窗 + 5 项故障注入 |
| PERF | SQL 性能预算 | **FAIL** | 否（风险披露） | `PERF_SQL_BUDGET_NOT_MET`：53 > 25 |
| DATA | 历史/题材/竞价缺口 | **NOT_COMPARABLE** | 否（原口径） | 7 条 NOT_COMPARABLE 保留 |

## 阻断项明细

1. **G3/G4**：本机无 Hyper-V/KVM/VMware 独立 Linux VM；按任务书未在宿主/WSL 应用防火墙，
   仅交付 `deploy/s6/vm/prepare-hyperv-vm.ps1` 与验证脚本。
2. **G8**：登录成功后 URL 停留 `/login`、`#root` 清空，刷新后恢复；3 次独立复现，
   根因未定位，不能判 PASS。
3. **SOAK**：24h 无法执行，`S6_STAGING_SOAK_PASSED = NOT_RUN`；短窗结果只作为
   拆分形态可用性证据，不冒充长稳。
4. **S6_SECURITY_REGRESSION_PASSED = NOT_VERIFIED**：应用层 50 条测试通过，
   OS 层未验证（受 G3/G4 阻断）。

## 非阻断保留项

- `PERF_SQL_BUDGET_NOT_MET`：workbench 53 SQL / 25 预算；
- 7 条 `NOT_COMPARABLE`；
- `THEME_REAL_DATA_ACCEPTANCE_PENDING`、`REAL_AUCTION_DATA_NOT_VERIFIED`、
  `HISTORICAL_DATA_GAP_UNDER_REVIEW` 均未关闭。

## 结论

强制 Gate（G0/G3/G4/G7/G8/G9/G10/SOAK）未全部 PASS，`S6_CANARY_READY` 不成立。
保持 `S6_CANARY_BLOCKED`：不合并发布分支、不部署生产、不开放灰度入口。
