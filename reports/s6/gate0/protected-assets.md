# S6 Gate 0：保护资产

- 采集时间：2026-11-15T01:11:10+08:00
- 机器可读：`reports/s6/gate0/protected-assets-gate0.json`
- JSON SHA256：`ee7442fec5aba9ec438c9981d908cb9e2da869d96a24062c487fa95ccb9be12c`

## 1. C0 五个脚本（Git blob）

| 文件 | blob |
| --- | --- |
| `scripts/forward_collect_tushare.py` | `1408f9e3253f0c353023863821ad70544f0fdf2a` |
| `scripts/forward_store.py` | `8064d6709a99b07e853c09c297bf26ad938fab5f` |
| `scripts/forward_track_c0.py` | `6ff3eb0861cb52580c2e20be2be778cf5bfb9abe` |
| `scripts/forward_universe.py` | `2f8979d299f2537427e3ab3c3031a2cb14d22ca0` |
| `backend/app/services/factor_score.py` | `32b6f2ff18373ece44f278a6e76db988385be80f` |

## 2. 冻结信号 SHA256

| 文件 | SHA256 |
| --- | --- |
| `backend/data/forward_c0/signals_2026-11-11.jsonl` | `1f80d1b6785b930fc9a922f027a323a36f5ddcf122d2ec865810f33523f07b37` |
| `backend/data/forward_c0/signals_2026-11-11.meta.json` | `44301eb5e00867e4e19d51c7dea524af0897ced0e7a5002cac2f530e3fa2dd50` |

## 3. 数据库（只读 + immutable）

| 库 | 大小（B） | mtime | SHA256 | integrity |
| --- | --- | --- | --- | --- |
| `backend/data/fupan.db` | 513,519,616 | 2026-10-31T22:26:44+08:00 | 未计算（>200MB，见限制） | ok |
| `backend/data/history.db` | 869,801,984 | 2026-10-20T14:14:38+08:00 | 未计算（>200MB） | ok |
| `backend/data/b1_research.db` | 1,441,378,304 | 2026-11-09T12:23:12+08:00 | 未计算（>200MB） | ok |
| `backend/data/forward_research.db` | 67,055,616 | 2026-11-11T17:17:58+08:00 | `c464b3bc751fc05361293a1e3537bcdba2ec5ffbe5a26105b737ea24f6ec536b` | ok |

**限制**：超过 200MB 的库按任务书要求记录大小/mtime/integrity，未做整文件 SHA256；
文件级一致性由 mtime + size + `PRAGMA integrity_check` 交叉验证。

## 4. 其它保护资产

- `forward_c0/*`、B1v 清单、Top20 规则、评价器：Git 状态为空（无修改、无 untracked）；
- `deploy/*`：本轮只新增 `deploy/s6/*` 脚本，未修改既有部署脚本与调度配置；
- 现有备份：未读取、未覆盖。

## 5. 校验方式

```powershell
.venv\Scripts\python.exe scripts\s6_gate0_fingerprint.py --stage gate0
```

脚本以 `mode=ro&immutable=1` 打开数据库，仅执行 `PRAGMA integrity_check`，
不 checkpoint、不建表、不写 WAL。
