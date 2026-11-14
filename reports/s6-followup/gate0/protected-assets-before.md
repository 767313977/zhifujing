# Gate 0：保护资产指纹（before）

- 生成时间：2026-11-15T02:02:58+08:00
- 生成命令：`python scripts/s6_gate0_fingerprint.py --stage before --out-dir reports/s6-followup/gate0`
- 采集时 Git：`5db5246010c884a8a980e8279e4b49cb164a3603`（branch v2；指纹采集后即新建 v3 分支，内容相同）
- 结论：`all_protected_assets_present = true`，`missing_assets = []`，`integrity_failed = []`
- JSON SHA256：`bc834c03ab5395c8c979b33b4f484473c90dcef2b5a635a27c5eb9ad7029fd75`

## 1. 与上轮 gate0 指纹的逐项对比

使用脚本对上轮 `reports/s6/gate0/protected-assets-gate0.json` 与本轮
`protected-assets-before.json` 做字段级对比（`c0_scripts` / `frozen_signals` /
`databases` 的 `size`、`mtime`、`sha256`、`integrity_check`）：

```text
diffs = 0
```

即 C0 五脚本 Git blob 与工作区 SHA256、两份冻结信号 SHA256、四个数据库的
大小/修改时间/SHA256/integrity 全部未变。

## 2. C0 五个脚本（Git blob + 工作区 SHA256）

| 文件 | git blob | worktree sha256 |
| --- | --- | --- |
| `backend/app/services/factor_score.py` | `32b6f2ff18373ece44f278a6e76db988385be80f` | `7063bb9dc0db7f88f04a869db097d1d211292ce1e27635ca9c2f0da412d6c88e` |
| `scripts/forward_collect_tushare.py` | `1408f9e3253f0c353023863821ad70544f0fdf2a` | `3aec889004a7f7d3c5e6d09a4c4279ab280bdb45ae90eac30c6b44221465e992` |
| `scripts/forward_store.py` | `8064d6709a99b07e853c09c297bf26ad938fab5f` | `f4b0a8ea4de5eed10cf9029d79b39589929396bea7615d3bce0895d34ffe87e1` |
| `scripts/forward_track_c0.py` | `6ff3eb0861cb52580c2e20be2be778cf5bfb9abe` | `9d20a569bacbb2c620995387d01bc0118a711a3d927d34623fb572d11656d727` |
| `scripts/forward_universe.py` | `2f8979d299f2537427e3ab3c3031a2cb14d22ca0` | `fb07ce84c79f7faaa733d33eb3d011d269b9e72f8a79e399473d00a7ada9dc3f` |

## 3. 冻结信号

| 文件 | SHA256 |
| --- | --- |
| `backend/data/forward_c0/signals_2026-11-11.jsonl` | `1f80d1b6785b930fc9a922f027a323a36f5ddcf122d2ec865810f33523f07b37` |
| `backend/data/forward_c0/signals_2026-11-11.meta.json` | `44301eb5e00867e4e19d51c7dea524af0897ced0e7a5002cac2f530e3fa2dd50` |

## 4. 数据库

| 库 | size | mtime | SHA256 | integrity |
| --- | --- | --- | --- | --- |
| `backend/data/fupan.db`（生产研究库） | 513,519,616 | 2026-10-31T22:26:44+08:00 | 不计算（>200MB） | ok |
| `backend/data/history.db` | 869,801,984 | 2026-10-20T14:14:38+08:00 | 不计算（>200MB） | ok |
| `backend/data/b1_research.db` | 1,441,378,304 | 2026-11-09T12:23:12+08:00 | 不计算（>200MB） | ok |
| `backend/data/forward_research.db` | 67,055,616 | 2026-11-11T17:17:58+08:00 | `c464b3bc751fc05361293a1e3537bcdba2ec5ffbe5a26105b737ea24f6ec536b` | ok |

WAL/SHM：`fupan.db` WAL 0 字节 + SHM 32768；`b1_research.db` WAL 0 字节 + SHM 32768；
`history.db`、`forward_research.db` 无 WAL/SHM。原始输出见
[database-integrity-before.txt](./database-integrity-before.txt)。

## 5. 方法限制

- 数据库以 `mode=ro&immutable=1` 打开：不读取未 checkpoint 的 WAL 内容，
  `integrity_check` 反映主库文件本身；WAL/SHM 存在性与大小单独记录。
- `b1_research.db` 超过 200MB，按任务书只记录大小/mtime + integrity，
  以此作为对照依据。
- 生产研究库 `fupan.db` 的 SHA256/size/mtime 必须在本轮后续每个阶段结束时
  与本节一致（见后续 `protected-assets-after.json` 与最终 Gate）。
