"""B1 候选范围清单：从已批准的历史研究库生成「证券身份清单」。

## 为什么需要这份清单

历史回测的候选池是「`history.db` 里出现过的全部代码」。前瞻如果只按
`code.startswith("920")` 放行北交所，就会把历史里从未出现过的北交所股票自动纳入，
两份研究就不是同一对象了。所以：

- **前瞻池 = 历史池 ∩ 身份可确认的证券**；
- 身份无法确认的证券（本轮只有 920 段的 32 只北交所）**不生成正式前瞻信号**；
- 清单带哈希与数据截止日，任何变动都必须换版本号。

## 901 段为什么默认不算「已确认」

北交所 2025-10-09 完成存量代码向 920 段切换。离线只能看到 `history.db` 里 920 代码与
名称，看不到「旧代码 ↔ 新代码」的官方映射；仅凭 `startswith("920")` 放行全部 920
等于把历史上没出现过的股票悄悄纳入。所以默认 `identity_verified = false`，
等交易所/供应商确认映射后再由人工把该证券标记为可前瞻。

## 用法

    python scripts/forward_universe.py --db backend/data/history.db \\
        --out backend/data/c0_universe_b1.json

只读打开 `history.db`；不联网、不写业务库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
for _path in (ROOT, BACKEND):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models import StockDaily  # noqa: E402
from scripts.forward_store import board_of  # noqa: E402

UNIVERSE_VERSION = "b1-2026-11-11"

#: 本轮不自动纳入的证券：北交所 920 段。等官方映射确认后逐个放开。
UNVERIFIED_BOARDS = {"北交所"}


def _readonly_engine(path: Path):
    return create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true")


def build_manifest(session: Session, *, source_db: str, generated_at: str) -> dict:
    cutoff: date = session.scalar(select(func.max(StockDaily.trade_date)))
    rows = session.execute(
        select(
            StockDaily.code,
            func.max(StockDaily.name),
            func.min(StockDaily.trade_date),
            func.max(StockDaily.trade_date),
            func.count(),
        ).group_by(StockDaily.code)
    ).all()

    securities = []
    for code, name, first_date, last_date, count in rows:
        board = board_of(code)
        verified = board not in UNVERIFIED_BOARDS
        securities.append(
            {
                "code": code,
                "name": name,
                "board": board,
                "first_date": first_date.isoformat(),
                "last_date": last_date.isoformat(),
                "rows": int(count),
                "identity_verified": verified,
                "forward_eligible": verified,
                "identity_note": (
                    "920 段为 2025-10-09 切换后的新代码；旧代码映射需交易所/供应商确认"
                    if board == "北交所"
                    else "沪深代码未发生段位迁移"
                ),
            }
        )
    securities.sort(key=lambda item: item["code"])

    payload = "\n".join(
        f"{item['code']}|{item['name']}|{item['board']}" for item in securities
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    by_board: dict[str, int] = {}
    for item in securities:
        by_board[item["board"]] = by_board.get(item["board"], 0) + 1
    eligible = sum(1 for item in securities if item["forward_eligible"])

    return {
        "version": UNIVERSE_VERSION,
        "source_db": source_db,
        "data_cutoff": cutoff.isoformat() if cutoff else None,
        "generated_at": generated_at,
        "universe_sha256": digest,
        "counts": {
            "total": len(securities),
            "forward_eligible": eligible,
            "excluded_unverified": len(securities) - eligible,
            "by_board": by_board,
        },
        "policy": (
            "前瞻池 = 历史池 ∩ 身份可确认证券；北交所 920 段在官方新旧代码映射确认前"
            "不生成正式前瞻信号；不自动纳入历史集合以外的新证券。"
        ),
        "securities": securities,
    }


def eligible_codes(manifest: dict) -> list[str]:
    return [item["code"] for item in manifest["securities"] if item["forward_eligible"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 B1 候选范围清单（只读历史库）")
    parser.add_argument("--db", default=str(BACKEND / "data" / "history.db"))
    parser.add_argument("--out", default="", help="输出 JSON 路径；留空只打印摘要")
    parser.add_argument("--verify", default="", help="校验已有清单的 universe_sha256")
    args = parser.parse_args(argv)

    if args.verify:
        if not args.out:
            raise SystemExit("--verify 需要同时指定 --out（要校验的清单文件）")
        manifest = json.loads(Path(args.out).read_text(encoding="utf-8"))
        actual = manifest.get("universe_sha256")
        print(f"清单版本 {manifest.get('version')} 截止 {manifest.get('data_cutoff')}")
        print(f"  实际 sha256 {actual}")
        print(f"  期望 sha256 {args.verify}")
        print("OK" if actual == args.verify else "MISMATCH")
        return 0 if actual == args.verify else 1

    db_path = Path(args.db)
    if not db_path.is_file():
        raise SystemExit(f"没有 {db_path}")

    engine = _readonly_engine(db_path)
    try:
        with Session(engine) as session:
            manifest = build_manifest(
                session,
                source_db=str(db_path),
                generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
    finally:
        engine.dispose()

    counts = manifest["counts"]
    print(f"清单 {manifest['version']}：截止 {manifest['data_cutoff']}")
    print(
        f"  总数 {counts['total']}，可前瞻 {counts['forward_eligible']}，"
        f"排除未确认 {counts['excluded_unverified']}"
    )
    for board, number in sorted(counts["by_board"].items()):
        print(f"    {board}: {number}")
    print(f"  sha256 {manifest['universe_sha256']}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"  已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
