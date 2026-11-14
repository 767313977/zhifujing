# S6 灰度就绪报告（Canary Readiness）

- 分支：`release/s6-canary-hardening`
- 基线：`104dfdf`（含 `3060e82`；S5 第四轮 5 个提交）
- 生成时间：2026-11-15
- **最终状态：`S6_IMPLEMENTATION_PARTIAL` / `S6_CANARY_BLOCKED` /
  `OS_EGRESS_DENY_NOT_VERIFIED` / `PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`**
- **不满足** `S6_CANARY_READY_PENDING_EXPLICIT_PRODUCTION_APPROVAL`

## 1. Gate 总表

| Gate | 要求 | 结果 | 证据 |
| --- | --- | --- | --- |
| G0 | 基线正确、工作区 clean、保护资产已记录 | **PASS** | `gate0/*` |
| G1 | 网络出口全局盘点 | **PASS** | `security/egress-inventory.md`、`egress-callgraph.json` |
| G2 | 应用层网络控制 fail closed | **PASS** | `security/egress-test-report.md`（35 条） |
| G3 | OS/网络层出站默认拒绝有真实证据 | **`NOT_VERIFIED`** | `security/os-policy-verification.md` |
| G4 | DNS/IPv4/IPv6/TCP/UDP/子进程路径已验证 | **`NOT_VERIFIED`** | 同上 |
| G5 | URL/Token/密钥脱敏 | **PASS** | `security/secret-redaction-report.md` |
| G6 | 启动锁定与 readiness 正确 | **PASS** | `runtime/startup-readiness.md` |
| G7 | Canary 数据库只读隔离真实通过 | **FAIL** | `runtime/database-isolation-report.md` |
| G8 | 调度器与外部抓取默认关闭 | **PASS**（配置层面） | `runtime/environment-matrix.md` |
| G9 | 质量状态不被篡改，7 条 NOT_COMPARABLE 保留 | **PASS** | `data-quality/not-comparable-register.md` |
| G10 | 后端/前端/浏览器全量回归 | **PASS** | `S6-acceptance-report.md` |
| G11 | 真实库只读对账无新增 unexpected | **PASS（exit 2）** | `data-quality/contract_matrix_v4.json` |
| G12 | Staging 部署、重启、回滚演练 | **PASS（本地隔离）** | `staging/*` |
| G13 | C0、冻结信号、三库保护资产不变 | **PASS** | `gate0/protected-assets-after.json` |
| G14 | 灰度方案、风险登记、审批材料完整 | **PASS** | `release/*` |

## 2. 阻塞项（必须先解决）

1. **OS 级出站默认拒绝未验证**（G3/G4）：没有隔离网络环境执行 nftables/网络命名空间
   测试；`deploy/s6/*` 只是脚本，未加载。
2. **数据库只读隔离未落地**（G7）：应用引擎仍读写同一 `fupan.db`，用户/会话/日志
   与研究表同库；灰度需要「只读研究库 + 独立可写用户库」。
3. 子进程出口（`lark-cli`）应用层不可控，必须靠 OS 策略或禁用；灰度方案已禁用。

## 3. 非阻塞但必须披露

- 53 SQL > 25 预算（P50 ≈ 136–165 ms）；
- 7 条 `NOT_COMPARABLE` 保留；
- 竞价真实数据、题材真实覆盖、历史缺口仍在审查；
- 依赖漏洞扫描未执行（无 pip-audit / 未联网 npm audit）。

## 4. 结论

本轮完成了应用层安全边界、readiness 契约、只读能力验证、数据质量登记与发布材料，
但 **OS 层与数据库隔离两项 P0 未通过**，因此不能申请生产灰度。
