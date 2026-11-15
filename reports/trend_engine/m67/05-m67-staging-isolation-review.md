# M6.7 Staging 隔离审查

- 既有实现：`backend/app/services/trend_engine/multisource.py`（M6.5）
- 本阶段**未修改** staging 代码，只做审查与结论

---

## 1. 隔离机制现状

| 机制 | 实现 | 状态 |
| --- | --- | --- |
| 唯一批次目录 | `StagingBatch` 目录存在即 `StagingViolation` | ✅ |
| 批次名白名单 | 正则 `[0-9A-Za-z_-]+` | ✅ |
| 原始库黑名单 | `assert_not_research_db` 拦截四个库名 | ✅ |
| 只追加 | `raw_payload_manifest` 唯一约束，重复插入抛 `IntegrityError` | ✅ |
| 多供应商隔离 | 主键含 provider，同事件不覆盖 | ✅ |
| 原始/规范化分离 | `raw_payload_manifest` 与业务表分开 | ✅ |
| 许可闸门 | `license_scope` 默认 `NOT_AUTHORIZED` | ✅ |
| 不自动合并 | 无合并代码路径 | ✅ |
| 不触发调度 | staging 模块无调度依赖 | ✅ |
| 不给模型服务 | staging 路径不在 API/训练路径 | ✅ |
| WAL/SHM 审计 | M6.6 已记录边车文件；M6.7 只做字节级哈希 | ⚠️ 见第 2 节 |

## 2. WAL/SHM 生命周期（M6.7 新增约束）

M6.5 Batch 6 只用 `mode=ro` 打开 M6.4 staging 时，SQLite 创建了 `-shm`（32,768 字节）与 0 字节 `-wal`。M6.7 的处理：

1. 计算哈希只做**字节级读取**，不打开 SQLite；
2. 如必须查询，使用 `?immutable=1` 只读 URI；
3. 新增边车文件必须登记到 manifest，不得静默出现；
4. 不删除已有边车（删除属于对证据目录的写操作）。

## 3. 本阶段写入边界

| 路径 | 是否使用 |
| --- | --- |
| `backend/data/m67_sandbox/` | 本阶段未创建（无离线派生产物需求） |
| `backend/data/m66_staging/...` | 只读核对，未写入 |
| 四个原始库 | 只读，未写入 |

## 4. 结论

staging 隔离机制满足 M6.7 要求；唯一需持续注意的是 SQLite 边车文件的生命周期。未授权供应商的真实 payload 未被持久化，符合「许可未确认不得落盘」。
