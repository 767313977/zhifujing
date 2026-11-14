# S5 发布前只读门禁与回滚预案（**未执行部署 / 未执行 revert**）

- 生成日期：2026-11-14（Asia/Shanghai）
- 分支：`feature/stock-screening-v2`
- 授权边界：不合并 master、不部署、不重启线上、不执行 `git revert` / DB restore
- 基线：`395ae3a`（S4 报告提交）；S5 提交见 `S5-acceptance-report.md`

## 1. API / Schema 兼容性

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| `FUPAN_SCHEMA_MODE=verify` | 通过（临时库） | 启动日志：`Schema 检查通过（verify，未执行任何 DDL）` |
| 生产库 schema | **未验证** | 未打开生产库；没有自动 migrate，也不允许自动 migrate |
| 新表 / 新列 | 无 | S5 未新增模型列，只新增只读服务与接口 |
| 响应字段 | 向后兼容 | 新增 `cutoff_at` / `end_of_day_fact` / `quality_version` 等字段；旧字段保留 |
| 新接口 | `GET /api/research/workbench`、`GET /api/quality/overview|timeline` 扩展 | 均需登录；GET 无 CSRF 风险 |

## 2. 新增只读路由审查

| 项 | 结论 |
| --- | --- |
| 鉴权 | `/api/research/workbench`、`/api/quality/*` 均 `Depends(current_user)` |
| 参数上限 | `limit ≤ 100`、`offset ≤ 1e6`、`days ≤ 250`、板块 ≤ 10、`mode/market/view` 白名单 |
| 日志脱敏 | 质量台只返回来源名/状态/原因码；不返回 SQL、磁盘路径、连接串、token |
| SQL 预算 | 见 `reports/s5_performance/benchmark.md`；workbench 50 条（与板块数无关） |
| 连接释放 | FastAPI `get_db` 依赖负责关闭 Session |
| 外呼 | 新路由只 SELECT；无 `sources/*` 调用，无 `syncStock`，无缓存写入 |

## 3. 与 `origin/master` 的关系（只读对比）

- `git log --oneline origin/master..HEAD`：98 个提交（S1–S5 全部功能分支内容）；
- S5 相对 S4 基线的改动仅涉及：`backend/app/services/*`、`backend/app/api/{stock,quality,research}.py`、
  `backend/app/schemas.py`、`backend/app/external_guard.py`、`backend/app/main.py`、
  `frontend/src/**`、`tests/**`、`scripts/**`、`reports/s5_*`、`S5-*.md`；
- 未改动：`deploy/*`、调度配置、`forward_c0/*`、C0 五脚本、生产数据库、真实来源采集协议；
- **不执行 merge**。

## 4. 回滚预案（候选，未执行）

S5 不涉及数据库迁移，回滚 = 代码回退。建议按提交逆序 revert，保留历史：

```powershell
# 候选版本：S4 报告提交 395ae3a（代码行为等价于 S4 终稿 f758123）
git revert --no-commit <S5-提交5> <S5-提交4> <S5-提交3> <S5-提交2> <S5-提交1>
git commit -m "revert(s5): roll back s5 batch"
```

单包回退：

```powershell
git revert --no-commit <A 提交>   # PIT v3
git revert --no-commit <B 提交>   # 质量 v2
git revert --no-commit <C 提交>   # 工作台
git revert --no-commit <D 提交>   # 离线闸门
```

前后端不同时升级的兼容风险：

| 组合 | 风险 | 处置 |
| --- | --- | --- |
| 新后端 + 旧前端 | 旧前端忽略新增字段；`StockResearchDrawer` 有 `at_cutoff_known` 回落逻辑 | 可短暂共存 |
| 旧后端 + 新前端 | `/api/research/workbench` 404 → 工作台显示「加载失败」；PIT 缺少 `end_of_day_fact` → 不显示该区块 | 建议同版本部署；回滚时前后端一起回退 |
| 新前端 + 旧质量台字段 | 旧质量台仍读 `status` / `reason_codes` | 不破坏 |

阻断点：

- 生产库 schema 未核对 → 部署前必须由授权人员用 `FUPAN_SCHEMA_MODE=verify` 对生产副本预检；
- 未做 OS 级出网隔离 → `OS_EGRESS_DENY_NOT_VERIFIED`；
- 未合并 master / 未部署 → `PRODUCTION_DEPLOYMENT_NOT_AUTHORIZED`、`P13_RELEASE_NO_GO`。

## 5. 冻结与只读复核（S5 结束复测）

| 项 | 值 | 与 S4 一致 |
| --- | --- | --- |
| C0 五 blob | 见 `S5-dependency-inventory.md` 1.1 | 是 |
| 冻结信号 SHA256 | 见 `S5-dependency-inventory.md` 1.2 | 是 |
| 三库 integrity | fupan / b1 / forward 均 `ok` | 是 |
| 三库读取模式 | `mode=ro&immutable=1` / 临时副本 | 是 |

## 6. 离线隔离结论

- 进程内：`requests` + `socket` 双层闸门、来源入口闸门、启动锁定，均已实测（`reports/s5_security/*`）；
- 宿主 / 容器 / 防火墙：**未实测** → 保留 `OS_EGRESS_DENY_NOT_VERIFIED`；
- `network_attempts` 仅代表 requests 层，不代表全宿主网络调用总数。
