# S6 运维检查清单（Canary）

## 上线前

- [ ] 发布提交 SHA 与审批一致；工作区 clean
- [ ] `EXTERNAL_DATA_FETCH_ENABLED=false`、`SCHEDULER_ENABLED=false` 已写入 systemd
- [ ] `WARMUP_PATTERN_CACHE=false`、`ENABLE_DEV_CORS=false`
- [ ] `FEISHU_PUSH_ENABLED=false`、`FEISHU_WEBHOOK_URL` 空、`FEISHU_TARGET` 空
- [ ] `FUPAN_SCHEMA_MODE=verify`
- [ ] OS egress 策略已加载并 `verify-egress.sh` 全部 block 用例 PASS
- [ ] 只读研究库副本 + 独立用户库已就绪
- [ ] 数据库备份可读（`PRAGMA integrity_check=ok`）
- [ ] 保护资产指纹与 Gate 0 一致
- [ ] `/api/ready` 返回 200（当前会 503 → 不要上线）

## 运行中（每日）

- [ ] `/api/health` 200；`/api/ready` 200
- [ ] `/api/security/egress` 与 `/metrics` 口径：`egress_denied_total` 无异常增长
- [ ] `policy_errors=0`；`dns_denied_total` 只来自预期目标
- [ ] scheduler 仍关闭（日志 + 状态）
- [ ] 外部取数仍关闭（外部路由 503）
- [ ] 质量状态分布：无 `NOT_COLLECTED` 被显示成 0
- [ ] 数据库未被写入（保护资产 hash 不变）
- [ ] 错误率 / P95 在批准阈值内（未定义则记录 `NOT_DEFINED`）

## 回滚触发

见 `rollback-runbook.md` §触发条件；一旦触发，立即停止扩量并回滚。
