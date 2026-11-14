# S6 灰度部署 Runbook（**未执行**）

> 前置：G3/G4/G7 必须先通过；当前不满足，禁止按本 Runbook 上线。

## 0. 前置检查

1. `git rev-parse HEAD` = 审批通过的发布提交；工作区 clean；
2. 保护资产指纹与 `reports/s6/gate0/protected-assets-after.json` 一致；
3. OS egress 策略已在隔离环境通过 `deploy/s6/verify-egress.sh`；
4. 只读研究库副本与独立用户库已就绪；
5. 回滚包（上一不可变版本）可用。

## 1. 部署步骤

```bash
# 1) 备份数据库副本（只读副本 + 用户库）
# 2) 校验备份可读（PRAGMA integrity_check）
# 3) 安装固定版本依赖（requirements.txt + package-lock.json）
# 4) 校验配置：EXTERNAL_DATA_FETCH_ENABLED=false / SCHEDULER_ENABLED=false
# 5) 验证 OS egress 策略（verify-egress.sh 全 PASS）
# 6) 启动 Web（单 worker，127.0.0.1:8000）
# 7) 确认 scheduler 未启动（日志 + /api/ready）
# 8) 确认外部抓取未启动（/api/health 200，外部路由 503）
# 9) GET /api/health → 200
# 10) GET /api/ready → 必须 200（当前会 503，属阻塞）
# 11) 只读 API smoke（quality/research）
# 12) 浏览器 smoke（登录、质量台、工作台、竞价空态）
# 13) 安全负向测试（未登录 401、外部取数 503、策略拒绝）
# 14) 数据库只读验证（探针 + 应用引擎 URL）
# 15) 重启测试
# 16) 回滚演练
```

## 2. 启动后必须保存的证据

- 启动日志（不含 token）；
- `/api/ready` 响应（含 gates）；
- smoke 结果 JSON；
- 浏览器截图 + SHA256；
- 数据库 integrity_check；
- 保护资产 after 指纹。

## 3. 失败处理

- 任一步失败 → 停止扩量，按 `rollback-runbook.md` 回滚；
- 不得通过改预算、改状态、删测试来「修复」。
