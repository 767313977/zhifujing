# P1 整改报告：缓存修订水位 / B2.1 对账 / 性能复查 / 功能回归

> 日期：2026-11-09
> 分支：`feat/p0.2-research-ui`（HEAD `75271c8`）
> 状态：整改已实施并通过测试；本地 commit 在门禁通过后创建（SHA 见本轮交付回复）。
> **未 push、未合并、未部署、未改生产库结构**。

---

## 1. 工作区与变更清单

| 项 | 状态 |
| --- | --- |
| 基线 HEAD | `75271c8` `feat(research): add post-market trend and low-start research UI` |
| 分支 | `feat/p0.2-research-ui` |
| 本次提交 | 6 个修改 + 5 个新增（见下表） |

| 文件 | 类型 | 内容 |
| --- | --- | --- |
| `backend/app/services/data_revision.py` | 新增 | 修订水位表/触发器/库身份/fail-closed 状态机 |
| `backend/app/services/research_screen.py` | 修改 | fail-closed 缓存、版本前后校验、single-flight、深拷贝、`data_latest` |
| `backend/app/api/research.py` | 修改 | `/meta` 返回缓存诊断状态 |
| `scripts/install_data_revision.py` | 新增 | 隔离库安装/查看/卸载（默认拒绝主库） |
| `tests/test_data_revision.py` | 新增 | 缓存安全矩阵，共 25 个用例 |
| `tests/test_research_screen.py` | 修改 | 数据时点/空库/路由鉴权/未安装不缓存（共 9 个） |
| `frontend/src/api/types.ts` | 修改 | 三个研究响应增加 `data_latest` |
| `frontend/src/pages/Research.tsx` | 修改 | 历史数据提示、未来日期警示、搜索范围说明 |
| `frontend/src/pages/__tests__/Research.test.tsx` | 修改 | 新增 6 个交互/边界用例（共 15 个） |

---

## 2. 缓存一致性与失效机制（R5 加固后）

### 2.1 真实表依赖矩阵

| 接口 | 响应字段 | 依赖表 | 查询条件 | 计算函数 | 修订域 |
| --- | --- | --- | --- | --- | --- |
| `/meta` | 策略文案 | 无（+ 缓存诊断读 sqlite_master） | — | 常量 | — |
| `/trend` | 代码/名称/收盘/均线/RPS/趋势天数/量比/距高点/依据 | `rps_daily`、`stock_daily`、`stock_basic`、`sector_member`、`trade_calendar` | `rps60 ≥ 阈值`、`trade_date = 目标日`、K 线窗口 ≤ 目标日 | `trend.load_bars`、`_ma/_slope/_trend_days` | rps、stock_daily、stock_basic、sector_member、trade_calendar |
| `/low-start` | 区间位置/平台/收敛/突破/量比/RPS20 | `stock_universe`、`stock_daily`、`stock_basic`、`rps_daily`、`trade_calendar` | 全市场、250 日窗口 ≤ 目标日 | `trend.load_bars`、`_low_start_rows` | stock_universe、stock_daily、stock_basic、rps、trade_calendar |
| `/sector-state` | RPS20/收益/成交额比/广度/核心股/数据日 | `sector_daily`、`sector_member`、`stock_daily` | `taxonomy`、最近 61 个板块日 | `_sector_states` | sector_daily、sector_member、stock_daily |
| 覆盖说明（未来 B 类） | 完整性状态 | `sector_member_backfill` | 目标日集合 | — | backfill_manifest |

**结论：原先 6 张触发器表不足**，缺少 `stock_universe` 与 `trade_calendar`。现已扩为 **8 个数据域**，覆盖上述全部读取表；`sector_member_backfill` 当前不参与响应值，但为后续 B 类指标预先纳入。

### 2.2 fail-closed 状态机（R5-01）

`data_revision.status()` 只返回三种状态：

| 状态 | 触发条件 | 缓存行为 |
| --- | --- | --- |
| `ready` | 修订表 + meta + **所有依赖表触发器齐全** + 版本可读 + 文件身份可确定 | 允许读写缓存 |
| `not_installed` | 缺修订表 / 缺 meta / 缺任一触发器 | 不读不写缓存，直接查库 |
| `unverifiable` | 版本读取异常、库身份无法确定 | 不读不写缓存，直接查库，并记录诊断日志（60 秒节流） |

- 数据库本身不可读 → 计算异常向上抛，**不返回旧缓存**；
- `/api/research/meta` 返回 `cache.state/reason/identity/revisions`，便于运维确认；
- `PRAGMA data_version` 与文件 stat 不再参与正确性判断，也不再作为缓存键。

### 2.3 修订水位设计

- `data_revision(domain, revision)` + `data_revision_meta(db_uuid)`；
- 8 个域、每张依赖表 `AFTER INSERT/UPDATE/DELETE` 触发器，同一事务内 `revision + 1`；
- 回滚时计数一并回滚；跨连接/跨进程提交后其他连接可见；
- 读版本一次 `SELECT`，与表规模无关。

### 2.4 快照一致性（R5-03）

计算前后各读一次 `status()`：

1. 版本一致 → 写入缓存；
2. 版本变化 → 丢弃结果重算（最多 3 轮）；
3. 仍不稳定或机制失效 → 本次返回结果但**不写缓存**。

对应测试 `test_n_write_during_compute_recomputes`：计算中另一连接提交后，结果重算为 `99.0`，缓存也存 `99.0`，不会把旧值标成新版本。

### 2.5 并发合并与前端加载

- 同一缓存键 single-flight：5 个并发请求只触发 1 次计算（`test_o_single_flight_merges_concurrent_calls`）；
- leader 异常时等待方超时后自行重算，不会拿到别人的失败结果；
- 前端已有「加载中…」状态，冷查询 6 秒期间有明确反馈。

### 2.6 触发器写入开销（R5-05，隔离副本）

50,000 行批量写入 `sector_member`（隔离副本，WAL）：

| 场景 | 耗时 | 修订推进 | WAL |
| --- | --- | --- | --- |
| 无触发器 INSERT | 1.45 s | — | 37.7 MB |
| 有触发器 INSERT | 3.99 s | +50,000 | 42.9 MB |
| 重复 UPSERT（同批） | 0.17 s | +50,000 | — |
| UPDATE 50k | 0.09 s | +50,000 | — |
| DELETE 50k | 1.34 s | +50,000 | — |
| 事务回滚（50k） | — | **+0** | — |

- 触发器确实是**逐行**推进（+50,000 而非 +1）；
- 全量 60 日约 170 万行，按此比例额外开销约 1~2 分钟，相对于回补总时长可接受，**本轮不做批级替代方案**；
- 未在正在回补的 `b1_research.db` 上安装/修改触发器，试验只在副本进行并已删除。

### 2.7 数据库身份（R5-04）

身份 = `(绝对路径, dev, inode, db_uuid)`：

- 不同路径副本即使 `db_uuid` 相同也不共用缓存（`test_i2`）；
- 同路径被新文件替换 → inode/`db_uuid` 变化（`test_i`）；
- 恢复旧备份到同路径 → inode 变化，缓存不复用；
- 重新打开同一库 → 身份不变，缓存继续命中（`test_i3`）；
- 文件被替换但连接池仍持旧连接时，身份来自当前路径 stat 与连接内 `db_uuid`，两者组合不会命中旧条目。

### 2.8 测试矩阵

`tests/test_data_revision.py` 共 **25** 个用例，覆盖：命中、参数隔离、历史 K 线修订、缺修订表、缺触发器、触发器恢复、版本不可读、成分股、RPS、manifest、回滚、跨文件、替换文件、同 UUID 不同路径、重新打开、跨进程、LRU/TTL、内存库、深拷贝、计算中写入、single-flight、数据库异常传播、逐行推进、幂等安装、触发器齐全。

⚠️ 修订水位**未安装到 `fupan.db`**；安装脚本默认拒绝主库，查看状态也只读打开。

---

## 3. B2.1 统计口径与对账（R1）

### 3.1 六类指标（统一定义）

| 指标 | 定义 | 快照值 |
| --- | --- | --- |
| 目标 board-day | 60 个目标日 × 当日 `sector_daily(kph_selected)` 板块数 | 16,190 |
| **日期全量完成率** | 当日所有有效板块均 complete 的天数 ÷ 60 | **17 / 60 = 28.3%** |
| **Board-day 完成率** | complete 的唯一 `(trade_date, taxonomy, sector_code)` ÷ 16,190 | **6,512 / 16,190 = 40.2%** |
| **Manifest 记录数** | manifest 中的不同 board-day 数 | 6,512 |
| **来源成分记录数** | 各行 `reported`（= `unique_count` = `written`）汇总 | 1,713,853 |
| **本地实际记录数** | 目标集合内的 `sector_member` 去重行数 | 1,713,853（目标集合内） |
| **Legacy 数据数** | 库内存在、但不属于当前 manifest 目标集合 | **229** |

### 3.2 「5,313 − 5,307 = 6」的解释

这是**两次非原子查询**在回补写入过程中产生的瞬时差，不是数据错误：

- 5,307 是「complete 计数」查询结果，5,313 是随后「manifest 总数」查询结果；
- 两次查询之间，正在执行的批次写入了 6 条 complete 记录；
- 在同一事务/一致性快照上重新统计：**manifest 6,512 = complete 6,512，差 0**；
- 当前 manifest 中非 complete 记录数为 **0**。
- 结论：不存在丢失或异常状态；报告口径已改为「同一快照内一次统计」。

### 3.3 「1,455,689 − 1,455,460 = 229」的解释

在一致性快照上按 `(trade_date, sector_code, code)` 做双向差集：

| 项 | 数值 |
| --- | --- |
| 目标窗口内 `sector_member` 总行数 | 1,714,082 |
| 其中属于 manifest 目标集合 | 1,713,853 |
| **Manifest 外 legacy** | **229** |
| Manifest 内但无成员 | 0 |
| 重复 `(date, sector, code)` | 0 |
| `reported` 与本地行数不一致的 board-day | 0 |
| 无法解释 | **0** |

229 行的构成（4 个 board-day）：

| 日期 | 板块代码 | 板块名 | 分类 | 行数 | 性质 |
| --- | --- | --- | --- | --- | --- |
| 2026-09-18 | 881121 | 半导体 | `kph_industry` | 182 | 行业分类，不在 `kph_selected` 目标集 |
| 2026-09-21 | 801653 | 霍乱概念 | `kph_selected` | 10 | 日常预取写入、未在 B1 top-40 清单内 |
| 2026-09-22 | 801992 | 文交所 | `kph_selected` | 6 | 同上 |
| 2026-09-24 | 881280 | 风电设备 | `kph_industry` | 31 | 行业分类，不在目标集 |

处理规则：229 行保留、不删除、不重复计入完成率；其中 16 行（霍乱概念/文交所）在 B2.1 回补到对应日期后会自然纳入 manifest，另外 213 行属于非目标分类，长期作为 legacy 保留说明。

### 3.4 当前覆盖（快照 2026-11-09 10:21）

- 已全量完成：17 天（07-03 ~ 07-23 连续，加 08-14、09-24）；
- 07-24 在快照时 250/269（随后已完成）；
- 其余 42 天仍为 B1 的 40 板块样本，**不计入全量**；
- 回补进程仍在按 5 日/批执行（批次 3 已完成，批次 4 进行中）。

---

## 4. 冷查询性能复查（R4）

### 4.1 执行计划（实测）

```
SEARCH stock_daily USING INDEX ix_stock_daily_code_date
       (code=? AND trade_date>? AND trade_date<?)
```

- 使用 `(code, trade_date)` 复合索引，`ORDER BY code, trade_date` 由索引顺序满足，**无临时 B-tree 排序**；
- 现有索引：`stock_daily(code, trade_date)`、`(trade_date, code)` 主键、`rps_daily(trade_date, code)`、`sector_member(sector_code, trade_date)`、`(code, trade_date)`；
- 未发现缺失索引，本轮**不新增索引**。

### 4.2 字段裁剪评估

原始 SQL：7 列 5.50 s vs 9 列 5.90 s（约 7%）。收益有限且 `build_bars` 为共享复权入口，裁剪需要改动共享加载器，本轮不实施。

### 4.3 性能统计（静态隔离快照，无并发写入）

| 接口 | 冷查询（5 个日期） | 热缓存 P50 | 热缓存 P95 |
| --- | --- | --- | --- |
| trend | 1.04 ~ 1.12 s | 1.67 ms | 2.19 ms |
| low-start | 6.00 ~ 6.21 s | 1.15 ms | 1.80 ms |
| sector-state | 0.11 ~ 0.27 s | 1.57 ms | 2.07 ms |

- 三个接口连续访问：冷 7.77 s → 热 0.006 s；
- low-start 不同 `limit`（50/100/200）冷查询 6.16~6.28 s：**耗时与返回条数无关**，瓶颈是全市场读取；
- 缓存失效后首次请求：6.11 s（见 §2.4）；
- 热缓存 P95 < 3 ms，满足「热缓存 <2 秒」目标；冷查询仍未达 2 秒，保留并标注限制。

### 4.4 优化前后一致性

优化前后三个接口各 120 行结果与基线 JSON **逐字段一致**；本轮缓存改造后再次全量回归通过（见 §6）。

---

## 5. 研究功能与历史时点回归（R3）

| 项 | 结果 |
| --- | --- |
| 数据时点 | 所有指标只用 ≤ 所选交易日的数据；`test_low_start_uses_only_data_up_to_trade_date` 验证截止日后暴涨不进入结果 |
| 历史数据提示 | 新增 `data_latest`；所选日早于库内最新时页面提示「历史数据：…这不是最新盘后结果」 |
| 未来日期 | 所选日晚于库内最新时页面明确警示「结果大概率为空」 |
| 搜索 | 代码/名称/行业；异常输入（`<script>!!`）走无匹配空态；页面注明「仅过滤当前返回的 N 条，不是全市场排名」 |
| 排序 | 升/降序、并列稳定、`null` 始终沉底；Tab 切换清空筛选，日期切换保留 Tab |
| 研究状态 | 满足/未满足/缺失/数据不足分开表达；固定标注「研究假设，未验证」 |
| 鉴权 | 研究路由在 OpenAPI 中存在，未加入 `PUBLIC_PATHS`，沿用 app 级 `require_login` |
| 参数校验 | `date` 为日期类型；`limit ≤200`；`taxonomy` 只允许两种；非法值返回 422 |
| 多用户隔离 | 缓存只存公共研究结果，不含自选/交易计划等用户数据 |

---

## 6. 测试结果（本轮实际执行）

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `python -m pytest -q` | **786 passed in 157.21s** |
| 修订水位 | `pytest tests/test_data_revision.py` | **25 passed** |
| 研究服务 | `pytest tests/test_research_screen.py` | **9 passed** |
| Ruff | `ruff check backend/app tests scripts` | All checks passed |
| 前端测试 | `npm run test` | **36 passed（4 files）** |
| 前端 Lint | `npm run lint` | 0 warnings / 0 errors（106 files） |
| 前端构建 | `npm run build` | 成功 |

---

## 7. 生产库未写入证据

| 证据 | 值 |
| --- | --- |
| `backend/data/fupan.db` 大小 | 513,519,616 字节 |
| `fupan.db` 最后修改时间 | 2026-10-31 22:26:44（本轮全程未变） |
| `fupan.db-wal` | 0 字节 |
| 写入目标 | `b1_research.db`、审计/性能隔离副本（均在 `backend/data`，已被 `.gitignore` 覆盖；副本已删除） |
| 安装脚本 | `--db` 必填；主库非 `--status` 直接拒绝；`--status` 以 `mode=ro` 打开 |
| 本轮执行 | 未执行 checkpoint、未执行生产迁移、未启动生产采集 |

---

## 8. 遗留问题

1. 冷查询仍约 6 秒，瓶颈是读取 132 万行日线；若要冷启动 <2 秒需做盘后预计算落表（需结构变更，待批准）。
2. 缓存是进程内缓存；多 worker 部署会各存一份（结果一致，仅内存重复），重启后首个请求为冷查询。
3. 未安装修订水位的库（当前含 `fupan.db`）按设计**关闭缓存**，每次请求为冷查询；这是有意的安全降级，不是故障。
4. 触发器为逐行推进：全量回补会增加约 1~2 分钟写库时间，已评估可接受。
5. B2.1 尚未完成（批次 1~5 已完成，批次 6 起继续），最终数据库一致性与覆盖率验收待全部批次结束。
6. B 类「全板块历史」指标保持门禁，未启用。
7. 未做真实浏览器端到端（登录态依赖），以组件测试 + 构建覆盖。

---

## 9. 上线条件建议

**不建议本轮进入生产发布。** 建议顺序：

1. 完成 B2.1 全部 60 日并通过最终审计；
2. 在隔离副本完成修订水位迁移演练后，另行申请主库结构变更；
3. 由独立审查确认研究指标口径与展示文案；
4. 再评估是否将 `/research` 纳入生产路由。

本轮所有改动仍只在开发分支和工作区，等待下一次独立审查。
