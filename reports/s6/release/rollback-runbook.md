# S6 回滚 Runbook

## 触发条件（任一）

1. OS egress 策略失效或发现未授权公网连接；
2. DNS 可绕过策略；
3. 生产数据库出现未授权写入；
4. 保护资产 hash 变化（C0 / 冻结信号 / 三库）；
5. 质量状态被错误转换（NOT_COLLECTED → 0 等）；
6. 认证/权限绕过；
7. 凭据泄露；
8. API 错误率/延迟超过已批准阈值（未定义则 `NOT_DEFINED`，不得编造）；
9. 备份不可恢复或回滚失败。

## 步骤

1. 停止 Canary Web（systemd `stop`），保留日志；
2. 切回上一不可变版本（代码 + 前端产物）；
3. 恢复配置（`EXTERNAL_DATA_FETCH_ENABLED=false`、`SCHEDULER_ENABLED=false`）；
4. 确认 OS egress 仍为默认拒绝（`verify-egress.sh`）；
5. 校验数据库完整性（研究库只读副本 + 用户库）；
6. 启动并执行只读 smoke + 浏览器 smoke；
7. 记录回滚时间、版本 SHA、结果。

## 本地演练结果（S6-E5）

见 `reports/s6/staging/rollback-drill.md`：
代码快照回滚（新版 → `HEAD`）后健康检查恢复、旧版无 `/api/ready`（404），
再切回新版 `/api/ready` 为 503（OS 门禁仍未验证）。数据库未被破坏。

## 限制

- 本演练不覆盖负载均衡/流量切换/生产 OS 策略恢复；
- 生产回滚必须由有权限的运维执行，TRAE 未获授权。
