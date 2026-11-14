# S4 浏览器验收证据

- 环境：临时 SQLite `%TEMP%\s4_e2e\s4.db`，临时账号 `s4a` / `s4b`；
- 后端：`127.0.0.1:8013`，`SCHEDULER_ENABLED=false`、`EXTERNAL_DATA_FETCH_ENABLED=false`、`FUPAN_SCHEMA_MODE=verify`；
- 浏览器：Chrome headless（CDP），视口 1280x800 / 1920x1080 / 3840x2160 / 390x844；
- 总计：25 项，通过 25 项。

## 用例结果

| 结果 | 用例 | 说明 |
| --- | --- | --- |
| PASS | theme_panel_ok | 题材交叉仍可用 |
| PASS | resonance_buckets | buckets=True up=True down=True again50=True |
| PASS | resonance_and_or_dedupe | and2=True or5=True |
| PASS | resonance_and_unverifiable | A01+A02+A04 -> UNVERIFIABLE |
| PASS | resonance_zero_sample | 09-21 无成员快照 -> 未采集，不伪造 0 只 |
| PASS | resonance_viewports | 1920/3840/390 screenshots captured |
| PASS | quality_overview | table=True reason=True |
| PASS | quality_detail_expand | 展开行显示原因码/最近尝试 |
| PASS | quality_filter | 筛选后隐藏可信来源 |
| PASS | quality_uncollected_day | 09-22 显示未采集 |
| PASS | quality_viewports | 1920/3840/390 screenshots captured |
| PASS | research_three_buckets | 三区标题存在 |
| PASS | research_later_not_in_known | known_has_theme=False unknown_has_theme=True |
| PASS | research_forward_outcome | D+1 -5.00% 不在已知区 |
| PASS | drawer_escape | Escape closes |
| PASS | drawer_refresh_asof | 刷新保留 stock/as_of |
| PASS | drawer_history | back_closed=True forward_open=True |
| PASS | limit_page | /limit-up 仍有个股链接 |
| PASS | auction_page | /auction |
| PASS | sentiment_page | /sentiment |
| PASS | account_switch_clears_drawer | user_b=True no_drawer=True |
| PASS | offline_gate_no_network | before_sent=0 after_sent=0 |
| PASS | offline_probe_note | status=200 note_offline=True |
| PASS | offline_sync_blocked_before_network | status=503 blocked=1 sent=0 |
| PASS | console_errors | 0 errors |

## 截图 SHA256

| 文件 | SHA256 | 字节 |
| --- | --- | --- |
| resonance_1280x800.png | `c713c46565f65591f6d5faadce79bd7cd045f04ae1c4acb86da1693275da1609` | 146939 |
| resonance_unverifiable_1280x800.png | `ca2a84d02012f4c0992ed8ce31e7e6247b4dfdb3085a3204ff9b60745939c386` | 151879 |
| resonance_1920x1080.png | `e9300740328228e23a13533b541de3df2a2b0c12d77e4834e83f70c93887876f` | 167657 |
| resonance_3840x2160.png | `3670cc6768d22a1f90efbe5929e10bdbc4e6e22ed45cfb45338659ce19840ce0` | 339444 |
| resonance_390x844.png | `a5818d5bff2de3f86ef518981c7fa7b50e9aac50dac5daa60708e440eb8f186e` | 59280 |
| quality_1280x800.png | `1995beaaa7195402afcad52a9ae271e75dc1c65217246bcc5e14fcbf25f90a72` | 104219 |
| quality_1920x1080.png | `9061ed767648eeaa012aa23eda3c1cd11dd7118ce090e3330336f672d2dfe8e9` | 100378 |
| quality_3840x2160.png | `6ee5592d7774bf0336433e1955008c93aa54b28d78e4a167b4d670622423eea6` | 124666 |
| quality_390x844.png | `e3dab734fee0ec7d9f4fdff1240c6aadd140abb5ea6b94ade43ac688b9f96672` | 57191 |
| research_three_buckets_1280x800.png | `a2ed6e359dc4e299133a3d1d49062d46cee9a5ce946570b3d064fe3d231d2cd1` | 152990 |

## 复现命令

```powershell
# 1) 造临时库（临时脚本，不提交）
.venv\Scripts\python.exe %TEMP%\s4_e2e\seed.py
# 2) 起隔离后端
$env:DB_PATH="%TEMP%\s4_e2e\s4.db"; $env:SCHEDULER_ENABLED="false"; $env:EXTERNAL_DATA_FETCH_ENABLED="false"; $env:FUPAN_SCHEMA_MODE="verify"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8013
# 3) 跑 Chrome headless
.venv\Scripts\python.exe %TEMP%\s4_e2e\drive.py
```

> 临时 harness（seed/drive）只用于本地验收，不提交；提交的是结果、截图哈希与 egress 计数。