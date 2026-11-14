# S6 灰度配置矩阵（建议值）

| 配置 | 灰度建议 | 说明 |
| --- | --- | --- |
| `EXTERNAL_DATA_FETCH_ENABLED` | **false** | 进程级锁定；所有外部取数 503 |
| `SCHEDULER_ENABLED` | **false** | 不启动定时采集 |
| `WARMUP_PATTERN_CACHE` | **false** | 避免未确认的重查询（生产开启会被 readiness 记 FAIL） |
| `FUPAN_SCHEMA_MODE` | **verify** | 启动只检查，不改库 |
| `FUPAN_ENV` | `production` | 用于策略环境匹配与配置冲突检查 |
| `ENABLE_DEV_CORS` | **false** | 不下发开发源 |
| `FEISHU_PUSH_ENABLED` | **false** | 不推送 |
| `FEISHU_WEBHOOK_URL` | **空** | 不配置 |
| `FEISHU_TARGET` | **空** | 禁用 lark-cli 子进程出口 |
| `IFIND_AUTH_TOKEN` | 可保留（不采集） | 不打印、不使用 |
| `KPH_LOCAL_ENABLED` | **false** | 不需要本机代理 |
| `DB_PATH` | 只读研究库副本 | **需要引擎支持 `mode=ro`（未实现）** |
| 用户数据存储 | 独立可写库 | **未实现** → G7 阻塞 |
| OS egress 策略 | 受控 proxy + 默认拒绝 | **未验证** → G3/G4 阻塞 |

## 启动前必须确认

1. `EXTERNAL_DATA_FETCH_ENABLED=false`、`SCHEDULER_ENABLED=false` 已写入 systemd
   （不要依赖默认值）；
2. `deploy/s6/egress-policy.json` 与将要部署的代码版本一致（策略版本 1）；
3. OS 策略已按 `deploy/s6/apply-egress-deny.sh` 应用并通过 `verify-egress.sh`；
4. `/api/ready` 在灰度前返回 200（当前必然 503，因为 OS 门禁未验证）；
5. 保护资产指纹与 Gate 0 一致。

## 明确不允许

- 通过 API 或热加载修改安全开关；
- 用环境变量伪造 `EGRESS_OS_POLICY_VERIFIED`；
- 把 `EXTERNAL_DATA_FETCH_ENABLED` 在运行期从 false 改回 true（代码已锁定）。
