"""Tushare 120 分档：单日 `daily` 只读验证（默认 dry-run，不联网）。

## 用途

只回答一个问题：**现有免费权限下，`daily(trade_date=20261019)` 能否一次拿到
C0 前瞻所需的全市场日线字段，并覆盖冻结的 5,108 只证券（含 32 只北交所）。**

## 安全边界

- **默认 dry-run**：只检查清单、依赖、参数，打印计划，**不发任何请求**；
- 只有显式加 `--execute` 才调用，且**只调用一次** `pro.daily`，不重试；
- 不写数据库、不建表、不建快照、不改任何既有文件（`--report` 是显式可选的 JSON）；
- Token 只从环境变量 / `.env` 读取，**任何输出都不打印**，打印前还会做一次脱敏替换。

## 用法

    # 只看计划（不联网）
    .venv\\Scripts\\python.exe scripts\\verify_tushare_daily.py

    # 授权后真正执行（仅一次请求）
    .venv\\Scripts\\python.exe scripts\\verify_tushare_daily.py --execute

前置：`pip install tushare`（当前环境未安装），并在 `.env` 或环境变量里配置
`TUSHARE_TOKEN`。**不要把 Token 贴到聊天、文档或代码里。**
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
for _path in (ROOT, BACKEND):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

DEFAULT_DATE = "20261019"
DEFAULT_MANIFEST = BACKEND / "data" / "c0_universe_b1_verified.json"
BSE_EVIDENCE = ROOT / "docs" / "b3" / "bse_identity_2026-11-11.json"

REQUIRED_FIELDS = ("open", "high", "low", "close", "pct_chg", "vol", "amount")

#: 与 `scripts/forward_store.board_of` 同一套代码段规则
BOARD_NAMES = ("沪主板", "科创板", "深主板", "创业板", "北交所", "其他")


def board_of(code: str) -> str:
    if code.startswith("60"):
        return "沪主板"
    if code.startswith("68"):
        return "科创板"
    if code.startswith("00"):
        return "深主板"
    if code.startswith("30"):
        return "创业板"
    if code[:2] in ("43", "83", "87", "88", "92"):
        return "北交所"
    return "其他"


def load_token() -> str:
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token
    env_file = ROOT / ".env"
    if env_file.is_file():
        from dotenv import dotenv_values

        value = (dotenv_values(env_file).get("TUSHARE_TOKEN") or "").strip()
        if value:
            return value
    return ""


def redact(text: str, token: str) -> str:
    if token and token in text:
        return text.replace(token, "***")
    return text


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(
            f"没有证券清单 {path}\n"
            "先运行：.venv\\Scripts\\python.exe scripts\\forward_universe.py "
            "--version b1v-2026-11-11 "
            "--bse-evidence docs\\b3\\bse_identity_2026-11-11.json "
            "--out backend\\data\\c0_universe_b1_verified.json"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def load_bse_codes(path: Path) -> list[str]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [item["code"] for item in payload.get("securities", [])]


def _to_code(ts_code: str) -> str:
    return ts_code.split(".")[0]


def analyse(df, manifest: dict, bse_codes: list[str]) -> dict:
    """把返回值折算成研究字段并做覆盖率核对（纯内存）。"""
    expected = {item["code"]: item["board"] for item in manifest["securities"]}

    missing_fields = [field for field in REQUIRED_FIELDS if field not in df.columns]
    rows = int(len(df))
    codes = [_to_code(value) for value in df["ts_code"]] if "ts_code" in df.columns else []
    unique = set(codes)
    duplicate_rows = rows - len(unique)

    missing = sorted(set(expected) - unique)
    extra = sorted(unique - set(expected))

    per_board: dict[str, dict] = {}
    for board in BOARD_NAMES:
        wanted = {code for code, value in expected.items() if value == board}
        if not wanted:
            continue
        got = wanted & unique
        per_board[board] = {
            "total": len(wanted),
            "returned": len(got),
            "missing": len(wanted - got),
            "coverage": len(got) / len(wanted) if wanted else 0.0,
        }

    bse_detail = []
    for code in bse_codes:
        row = df[df["ts_code"].astype(str).str.startswith(code)] if "ts_code" in df.columns else df.iloc[0:0]
        item = {"code": code, "returned": bool(len(row))}
        if len(row):
            record = row.iloc[0]
            item["ts_code"] = record.get("ts_code")
            item["close"] = float(record.get("close") or 0)
            item["pct_chg"] = float(record.get("pct_chg") or 0)
        bse_detail.append(item)

    # 单位换算只在内存里做，不落库；用于确认换算方向正确
    converted = None
    if not missing_fields and rows:
        sample = df.head(3)
        converted = [
            {
                "ts_code": record.get("ts_code"),
                "volume_shares": float(record.get("vol") or 0) * 100,
                "amount_yuan": float(record.get("amount") or 0) * 1000,
                "pct_chg": float(record.get("pct_chg") or 0),
            }
            for _, record in sample.iterrows()
        ]

    return {
        "rows": rows,
        "unique_codes": len(unique),
        "duplicate_rows": duplicate_rows,
        "missing_fields": missing_fields,
        "null_ratio": {
            field: (float(df[field].isna().mean()) if field in df.columns else None)
            for field in REQUIRED_FIELDS
        },
        "matched": len(set(expected) & unique),
        "expected_total": len(expected),
        "missing_count": len(missing),
        "missing_examples": missing[:50],
        "extra_count": len(extra),
        "extra_examples": extra[:20],
        "per_board": per_board,
        "bse_total": len(bse_codes),
        "bse_returned": sum(1 for item in bse_detail if item["returned"]),
        "bse_detail": bse_detail,
        "converted_sample": converted,
        "truncation_suspected": rows >= 6000,
    }


def print_report(result: dict, day: str) -> None:
    print(f"\n=== Tushare daily 验证 · {day} ===")
    print(f"返回行数 {result['rows']}，唯一代码 {result['unique_codes']}，"
          f"重复行 {result['duplicate_rows']}")
    if result["missing_fields"]:
        print(f"⚠️ 缺字段：{result['missing_fields']}")
    else:
        print("字段齐全：open / high / low / close / pct_chg / vol / amount")
    nulls = ", ".join(
        f"{key}={value:.2%}" for key, value in result["null_ratio"].items() if value is not None
    )
    print(f"空值率：{nulls}")

    print(f"\n清单匹配 {result['matched']}/{result['expected_total']}，"
          f"缺失 {result['missing_count']}，清单外 {result['extra_count']}")
    print("\n分市场覆盖：")
    for board, item in result["per_board"].items():
        print(f"  {board:<4} 应有 {item['total']:>4}  返回 {item['returned']:>4}  "
              f"缺 {item['missing']:>3}  覆盖 {item['coverage']:.2%}")

    print(f"\n北交所：{result['bse_returned']}/{result['bse_total']} 只返回")
    for item in result["bse_detail"]:
        if not item["returned"]:
            print(f"  缺 {item['code']}")

    if result["converted_sample"]:
        print("\n单位换算样例（vol×100→股，amount×1000→元）：")
        for item in result["converted_sample"]:
            print(f"  {item['ts_code']}  股 {item['volume_shares']:.0f}  "
                  f"额 {item['amount_yuan']:.0f}  pct {item['pct_chg']:.2f}%")

    if result["truncation_suspected"]:
        print("\n⚠️ 行数达到 6000，疑似触顶；需分段取。")
    if result["missing_examples"]:
        print(f"\n缺失代码（前 50）：{result['missing_examples']}")
    if result["extra_examples"]:
        print(f"\n清单外代码（前 20）：{result['extra_examples']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tushare daily 单日只读验证")
    parser.add_argument("--date", default=DEFAULT_DATE, help="交易日 YYYYMMDD")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--execute", action="store_true", help="真正发送唯一一次请求")
    parser.add_argument("--report", default="", help="可选：把结果写成 JSON（不写数据库）")
    args = parser.parse_args(argv)

    manifest = load_manifest(Path(args.manifest))
    bse_codes = load_bse_codes(BSE_EVIDENCE)
    token = load_token()

    print("计划：调用 1 次 daily(trade_date=%s)" % args.date)
    print(f"清单：{len(manifest['securities'])} 只（{manifest.get('version')}），"
          f"北交所 {len(bse_codes)} 只")

    if not args.execute:
        print("\n[dry-run] 未发送任何请求。")
        print("已就绪项：清单可读；Token " + ("已配置（不显示）" if token else "未配置"))
        try:
            import importlib.util

            has_tushare = importlib.util.find_spec("tushare") is not None
        except Exception:  # noqa: BLE001
            has_tushare = False
        print("依赖：" + ("tushare 已安装" if has_tushare else "tushare 未安装（pip install tushare）"))
        return 0

    if not token:
        raise SystemExit("未配置 TUSHARE_TOKEN（环境变量或 .env），已停止，未发送请求。")
    try:
        import tushare as ts
    except ImportError:
        raise SystemExit("未安装 tushare：先执行 pip install tushare，再重试；本次未发送请求。")

    pro = ts.pro_api(token)
    try:
        df = pro.daily(trade_date=args.date)
    except Exception as error:  # noqa: BLE001
        message = redact(str(error), token)
        raise SystemExit(f"接口调用失败（未重试）：{message}") from error

    result = analyse(df, manifest, bse_codes)
    print_report(result, args.date)

    if args.report:
        Path(args.report).write_text(
            json.dumps(
                {"date": args.date, "manifest_version": manifest.get("version"), **result},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n已写入 {args.report}（不含任何凭据）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
