"""C0 策略的前瞻纸面跟踪（本地、手动、只读）。

## 这个工具做什么

1. `snapshot`：为**库内最新交易日**生成一份冻结候选快照（Top20），写 JSONL + meta + 内容哈希。
2. `evaluate`：对已冻结的快照按 D+1 开盘入场、5 日收盘退出做评价，输出状态计数与收益。
3. `list`：列出已有快照。

## 这个工具不做什么

- 不建定时任务、不连网络、不连交易账户、不写业务库。
- 不为历史日期补生成快照（`day < 最新交易日` 直接拒绝）—— 避免事后用未来行情伪装成当日记录。
- 不修改生产权重，不改生产选股逻辑。

## 冻结协议

见 `PROTOCOL`。权重与候选池定义一起冻结；生产 `factor_score` 是**池内**评分，
本工具的候选池与网站某个既有选股池**不是同一个池**，不能把「公式相同」当成「策略相同」。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import StockDaily  # noqa: E402
from app.services import factor_score  # noqa: E402
from app.services.research_backtest import (  # noqa: E402
    Benchmark,
    Signal,
    load_matrix,
    run_signals,
)
from scripts.backtest_reversal import (  # noqa: E402
    _build_series,
    _code_columns,
    _factor_percentiles,
    _liquidity,
    _net,
)

logger = logging.getLogger("forward_c0")

HISTORY_DB = BACKEND / "data" / "history.db"
OUT_DIR = BACKEND / "data" / "forward_c0"

STRATEGY_VERSION = "c0-2026-11-10"
TOP_N = 20
HORIZON = 5
COSTS = (30.0, 50.0, 100.0)

#: 冻结协议 —— 任何一项变动都必须换 `STRATEGY_VERSION`，不能就地改
PROTOCOL: dict = {
    "strategy": "C0 原三因子（生产 factor_score 公式）",
    "weights": {"rev20": 0.30, "illiq20": 0.30, "amp20": 0.40},
    "window": 20,
    "min_coverage": 0.9,
    "universe_definition": (
        "history.db 内 D 日有行情、且 20 日窗口内有效行 >= 18 的全部代码；"
        "含主板/创业板/科创板/北交所，含 ST（ST 不剔除）；"
        "不套用生产 stock_universe 的流动性门槛，不套用市值上限"
    ),
    "liquidity_window": 20,
    "percentile_scope": "当日全体候选池内",
    "tie_break": "分数降序，同分按代码升序",
    "top_n": TOP_N,
    "signal_time": "D 日收盘后",
    "entry": "D+1 开盘",
    "exit": "第 5 个交易日收盘（引擎交易日定义，跌停顺延）",
    "costs_bps": list(COSTS),
    "data_source": "backend/data/history.db（SQLite mode=ro）",
}


def _readonly_engine(path: Path):
    return create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true")


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(Path(__file__).resolve().parent.parent),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _data_revision(session: Session, db_path: Path) -> dict:
    """数据版本证据：优先 data_revision 表，否则用文件 mtime + size。"""
    try:
        from sqlalchemy import text

        pairs = session.execute(
            text("SELECT domain, revision FROM data_revision ORDER BY domain")
        ).all()
        if pairs:
            return {"kind": "data_revision", "value": {k: int(v) for k, v in pairs}}
    except Exception:  # noqa: BLE001
        session.rollback()
    stat = db_path.stat()
    return {
        "kind": "file_stat",
        "value": {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size},
    }


def _paths(out_dir: Path, day: date) -> tuple[Path, Path]:
    return (
        out_dir / f"signals_{day.isoformat()}.jsonl",
        out_dir / f"signals_{day.isoformat()}.meta.json",
    )


def cmd_snapshot(args) -> int:
    db_path = Path(args.db)
    out_dir = Path(args.out)
    if not db_path.is_file():
        raise SystemExit(f"没有 {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = _readonly_engine(db_path)
    with Session(engine) as session:
        latest = session.scalar(select(func.max(StockDaily.trade_date)))
        if latest is None:
            raise SystemExit("研究库是空的")
        day = date.fromisoformat(args.date) if args.date else latest
        if day > latest:
            raise SystemExit(f"{day} 的数据尚未采集完成（库内最新 {latest}），拒绝冻结")
        if day < latest:
            raise SystemExit(
                f"{day} 早于库内最新交易日 {latest}：拒绝为历史日期补生成快照，"
                "避免事后用未来行情伪装成当日记录"
            )

        jsonl_path, meta_path = _paths(out_dir, day)
        if jsonl_path.exists():
            logger.warning("已存在 %s 的冻结快照，拒绝覆盖", day)
            return 2

        start = day - timedelta(days=90)
        matrix = load_matrix(session, start, day)
        if day not in matrix.date_pos:
            raise SystemExit(f"{day} 不在行情矩阵里（非交易日或无数据）")
        pos = matrix.date_pos[day]
        cols = _code_columns(matrix)
        j_of = {code: j for j, code in enumerate(matrix.codes)}

        series: dict[str, list[tuple]] = {}
        liquidity: dict[str, float] = {}
        for code in matrix.codes:
            if not matrix.present[pos, j_of[code]]:
                continue
            window = _build_series(matrix, cols, pos, code, j_of[code])
            if window is None:
                continue
            series[code] = window
            value = _liquidity(matrix, cols[code], pos, j_of[code])
            if value is not None:
                liquidity[code] = value
        if not series:
            raise SystemExit("当日没有可计算因子的候选")

        scores = factor_score.score_series(series)
        percent = _factor_percentiles(scores)
        ranked = sorted(
            ((code, item.score) for code, item in scores.items()),
            key=lambda pair: (-pair[1], pair[0]),
        )[: TOP_N]

        revision = _data_revision(session, db_path)
        db_stat = db_path.stat()
        created = datetime.now()

    created_at = created.astimezone().isoformat(timespec="seconds")
    if datetime.fromtimestamp(db_stat.st_mtime).isoformat(timespec="seconds") > created_at:
        raise SystemExit("数据库修改时间晚于当前时间，数据状态不可信，拒绝冻结")

    rows = []
    for rank, (code, total) in enumerate(ranked, start=1):
        item = scores[code]
        rows.append(
            {
                "signal_date": day.isoformat(),
                "created_at": created_at,
                "strategy_version": STRATEGY_VERSION,
                "git_commit": _git_commit(),
                "source_data_date": latest.isoformat(),
                "source_data_revision": revision,
                "universe_definition": PROTOCOL["universe_definition"],
                "universe_count": len(series),
                "stock_code": code,
                "rank": rank,
                "score_total": round(total, 1),
                "rev20": item.rev20,
                "illiq20": item.illiq20,
                "amp20": item.amp20,
                "percentiles": percent[code],
                "avg_amount_20": round(liquidity.get(code, 0.0), 2),
                "eligibility_reason": "20日窗口>=18行 + D日有行情",
            }
        )

    payload = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    # 二进制写入：避免 Windows 换行转换改变字节，导致内容哈希与文件不一致
    jsonl_path.write_bytes(payload.encode("utf-8"))
    meta = {
        "signal_date": day.isoformat(),
        "created_at": created_at,
        "strategy_version": STRATEGY_VERSION,
        "git_commit": rows[0]["git_commit"],
        "source_data_date": latest.isoformat(),
        "source_data_revision": revision,
        "protocol": PROTOCOL,
        "row_count": len(rows),
        "universe_count": len(series),
        "content_sha256": digest,
    }
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    engine.dispose()

    print(f"已冻结 {day}：{len(rows)} 只候选，池 {len(series)} 只，sha256={digest[:16]}…")
    print(f"  {jsonl_path}")
    print(f"  {meta_path}")
    return 0


def cmd_evaluate(args) -> int:
    db_path = Path(args.db)
    out_dir = Path(args.out)
    day = date.fromisoformat(args.date)
    jsonl_path, meta_path = _paths(out_dir, day)
    if not jsonl_path.is_file():
        raise SystemExit(f"没有 {day} 的冻结快照：{jsonl_path}")

    raw = jsonl_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("content_sha256") != digest:
        raise SystemExit("快照内容哈希与 meta 不一致：文件被改动过，拒绝评价")

    lines = raw.decode("utf-8").splitlines()
    signals = [
        Signal(code=row["stock_code"], signal_date=day, meta={"rank": row["rank"]})
        for row in map(json.loads, lines)
    ]

    engine = _readonly_engine(db_path)
    with Session(engine) as session:
        latest = session.scalar(select(func.max(StockDaily.trade_date)))
        matrix = load_matrix(session, day, min(latest, day + timedelta(days=60)))
        benchmark = Benchmark(session, matrix, "market")
        result = run_signals(
            session,
            signals,
            strategy=f"C0 forward {day}",
            params={"top": TOP_N, "version": STRATEGY_VERSION},
            horizons=(HORIZON,),
            entry_mode="next_open",
            cost_bps=0.0,
            matrix=matrix,
            benchmark=benchmark,
        )
    engine.dispose()

    stat = result.horizons.get(HORIZON)
    if stat is None:
        raise SystemExit("没有可评价的样本")
    groups = dict(stat.counts)
    ok = [s for s in result.samples if s.horizon == HORIZON and s.status == "ok"]
    entry = [
        s
        for s in result.samples
        if s.horizon == HORIZON and s.status in ("ok", "exit_limit_down")
    ]

    print(f"快照 {day}（{len(signals)} 只，哈希校验通过）")
    print("状态：" + "，".join(f"{k}={v}" for k, v in sorted(groups.items()) if v))
    if entry:
        gross = sum(s.gross for s in entry) / len(entry)
        print(f"已入场且有退出价 {len(entry)} 笔：毛均值 {gross:+.2f}%")
        for cost in COSTS:
            print(f"  {int(cost):>3}bps 净均值 {sum(_net(s.gross, cost) for s in entry) / len(entry):+.2f}%")
        bench = [s.benchmark for s in entry if s.benchmark is not None]
        if bench:
            print(f"  基准毛均值 {sum(bench) / len(bench):+.2f}%")
    if ok and len(ok) != len(entry):
        print(f"  其中正常完成 {len(ok)} 笔，延迟退出 {len(entry) - len(ok)} 笔")
    pending = groups.get("pending", 0)
    if pending:
        print(f"  未成熟（pending）{pending} 笔：不计入任何收益")
    print()
    print("口径：信号层（SIGNAL_LEVEL_ONLY）；毛/净均为已入场且退出可确定的样本；")
    print("      未完成退出不计收益；基准为同日等权毛收益，未扣费。")
    return 0


def cmd_list(args) -> int:
    out_dir = Path(args.out)
    if not out_dir.is_dir():
        print("还没有任何冻结快照")
        return 0
    for path in sorted(out_dir.glob("signals_*.jsonl")):
        meta_path = path.with_suffix(".meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        print(
            f"{meta.get('signal_date', path.stem)}  rows={meta.get('row_count', '?')}  "
            f"pool={meta.get('universe_count', '?')}  sha={str(meta.get('content_sha256', ''))[:12]}…"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C0 前瞻纸面跟踪（本地手动）")
    parser.add_argument("--db", default=str(HISTORY_DB))
    parser.add_argument("--out", default=str(OUT_DIR))
    sub = parser.add_subparsers(dest="cmd", required=True)

    snap = sub.add_parser("snapshot", help="为最新交易日生成冻结快照")
    snap.add_argument("--date", default="", help="默认取库内最新交易日；早于最新日会被拒绝")
    snap.set_defaults(func=cmd_snapshot)

    ev = sub.add_parser("evaluate", help="评价已冻结快照")
    ev.add_argument("--date", required=True)
    ev.set_defaults(func=cmd_evaluate)

    ls = sub.add_parser("list", help="列出已冻结快照")
    ls.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
