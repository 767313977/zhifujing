# 数据修订水位：生产迁移与回滚方案（仅设计，不执行）

> 日期：2026-11-09
> 状态：**未执行**。本轮不批准安装生产触发器。
> 目标：让研究缓存只在校验通过时启用，且历史日期修订能可靠失效。

---

## 1. 备份要求

1. 迁移前用 SQLite 在线备份 API 生成完整快照（含 WAL），校验 `PRAGMA quick_check` 为 `ok`；
2. 备份文件与迁移日志分开存放，至少保留两份（本机 + 异地/挂载盘）；
3. 记录迁移前：库大小、`page_count`、`freelist_count`、各表行数（`stock_daily`/`rps_daily`/`sector_daily`/`sector_member`/`stock_basic`/`stock_universe`/`trade_calendar`/`sector_member_backfill`）；
4. 备份未通过校验，不进入下一步。

## 2. 迁移前结构检查

```sql
-- 1) 确认没有同名对象（应为空）
SELECT name, type FROM sqlite_master
WHERE name IN ('data_revision','data_revision_meta')
   OR name LIKE 'trg\_%' ESCAPE '\';
-- 2) 确认依赖表齐全
SELECT name FROM sqlite_master WHERE type='table' AND name IN
 ('stock_daily','stock_basic','stock_universe','trade_calendar','rps_daily','sector_daily','sector_member','sector_member_backfill');
-- 3) 确认索引状态（迁移不改索引）
PRAGMA index_list('stock_daily');
PRAGMA index_list('sector_member');
```

## 3. 安装清单（8 个数据域 / 24 个触发器）

| 数据域 | 表 | 触发器 |
| --- | --- | --- |
| stock_daily | `stock_daily` | `trg_stock_daily_ai/au/ad` |
| stock_basic | `stock_basic` | `trg_stock_basic_ai/au/ad` |
| stock_universe | `stock_universe` | `trg_stock_universe_ai/au/ad` |
| trade_calendar | `trade_calendar` | `trg_trade_calendar_ai/au/ad` |
| rps | `rps_daily` | `trg_rps_daily_ai/au/ad` |
| sector_daily | `sector_daily` | `trg_sector_daily_ai/au/ad` |
| sector_member | `sector_member` | `trg_sector_member_ai/au/ad` |
| backfill_manifest | `sector_member_backfill`（若存在） | `trg_sector_member_backfill_ai/au/ad` |

- 触发器逻辑：`AFTER INSERT/UPDATE/DELETE` → `data_revision[domain] += 1`；
- 表不存在时跳过对应触发器（当前生产库若没有 manifest 表，只装 21 个）；
- 安装脚本：`scripts/install_data_revision.py --db <path>`（拒绝主库，需显式指定）。

## 4. 初始化逻辑

1. 建表 `data_revision(domain TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0)`；
2. 建表 `data_revision_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)`；
3. 写入 `db_uuid`（uuid4 hex，仅首次；重复执行不变）；
4. 建触发器（`IF NOT EXISTS`，幂等）；
5. 各域 revision 从 0 开始；不预填历史计数。

## 5. 写锁与安装耗时

- 全程一个事务，DDL + 建触发器在 SQLite 上是**毫秒到秒级**（大表只登记 schema，不扫描数据）；
- 安装期间持有写锁：必须选择**采集任务空闲窗口**（如收盘链路结束后、次日盘前）；
- 若与采集并发，写请求会等待 `busy_timeout`；超时会失败。迁移失败只回滚本次 DDL，不影响业务数据。

## 6. 与既有触发器的兼容

- 目前生产库没有业务触发器；若未来存在其他 `trg_*`，本方案使用固定前缀，不覆盖；
- 迁移前检查同名对象（见 §2）；发现同名但结构不同 → 停止并人工核对，不自动覆盖。

## 7. 并发采集期间的风险

| 风险 | 处理 |
| --- | --- |
| 安装事务与采集写锁冲突 | 只在空闲窗口执行；失败即中止 |
| 安装后立即采集，revision 快速增长 | 设计上允许；只带来缓存失效，不影响数据 |
| 触发器增加写开销 | 实测 5 万行 1.45→3.99 s（+175%）；全量回补额外约 1~2 分钟；需在发布说明中标注 |
| 触发器逐行推进 | 一批 N 行 = revision +N，不假设“一次批量只 +1” |

## 8. 失败与回滚

**安装失败**：事务回滚；确认 `sqlite_master` 无残留；不影响业务。

**需要回滚时（按顺序）**：

1. 先将应用回退到**不依赖 `data_revision` 的版本**（缓存自动 fail-closed，不影响研究结果正确性）；
2. 删除 24 个触发器（或存在的 21 个）；
3. 删除 `data_revision`、`data_revision_meta`；
4. 校验 `PRAGMA quick_check` 与关键表行数未变；
5. 恢复缓存需重新安装并自检。

⚠️ 不能只删表不删触发器（触发器引用缺失表会让**所有写入失败**）；也不能只回退应用而保留触发器（无害但会持续写入孤儿表，需一并清理）。

## 9. 启用缓存前自检

```sql
SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg\_%' ESCAPE '\';
SELECT * FROM data_revision_meta WHERE key='db_uuid';
SELECT domain, revision FROM data_revision;
```

- `/api/research/meta`（管理员）应返回 `cache.state=ready`；
- 普通用户不应看到 `cache` 字段；
- 抽样验证：修改一条历史 `stock_daily` → 对应域 revision 增加 → 研究接口重算。

## 10. 迁移后验证 SQL / 测试

1. `PRAGMA quick_check` = ok；
2. 关键表行数与迁移前一致；
3. 触发器数量与清单一致；
4. 数据修订：插入/更新/删除各一行 → revision 分别 +1；
5. 回滚测试（隔离库）：事务内写入后回滚 → revision 不变；
6. 缓存：冷/热/失效后首次请求的响应时间与结果一致性；
7. 多进程写入 → 版本可见；
8. 关闭缓存（临时移除一个触发器）→ 接口仍返回正确结果（只是变慢）。

## 11. 时间窗口与上线步骤（建议）

1. 收盘链路全部结束、备份完成后（如 23:30 之后）；
2. 执行迁移 → 自检 → 打开管理员诊断确认 `ready`；
3. 观察一轮交易日：错误率、响应时间、写锁等待；
4. 若异常：按 §8 回滚，应用保持 fail-closed 版本可继续使用（只是每次查库）。

## 11. 资源预算与部署前提（P0.5 补充）

| 项 | 事实/要求 |
| --- | --- |
| ASGI worker | 仓库部署脚本为 **1 个 uvicorn 进程**；限流与 single-flight 都是进程内的，生产必须保持单 worker 或另行设计共享协调 |
| 应用空闲内存 | 实测子进程 272.8 MB（峰值 425.2 MB） |
| 单次全市场扫描增量 | 流式实现约 **154 MB**；并发 2 约 247 MB（独立进程） |
| 建议预算 | 应用峰值 ≤ 0.8 GB；服务器 ≥ 4 GB 才考虑并发 2，≥ 2 GB 建议并发 1；采集任务峰值需在服务器实测 |
| 反向代理 | `proxy_read_timeout 600s`，冷查询 5.7~7.5 s 不会触发超时 |
| 进程内存限制 | 当前 systemd 未设 `MemoryMax`；迁移前应确认 cgroup 内存上限 |
| 迁移后首次冷查询 | 约 5.7~7.5 s、增量内存约 150 MB，属于发布后第一波访问的正常成本 |
| 触发器写放大 | 5 万行 1.45→3.99 s（+175%）；全量回补额外约 1~2 分钟 |

迁移前必须在服务器执行（只读）：

```bash
free -m
nproc
systemctl show fupan -p MemoryCurrent -p MemoryMax -p TasksCurrent
```

## 12. 本轮状态

- 迁移脚本、验证 SQL 已在**隔离副本**验证通过（含全量规模副本）；
- **生产库未安装任何触发器**，生产结构未变；
- 本方案需再次批准后才可执行。
