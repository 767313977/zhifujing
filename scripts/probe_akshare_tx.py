"""探测「腾讯日线」这条兜底源：字段 / 单位 / 可达性，并与库里 iFinD 口径对账。

用法:
    python scripts/probe_akshare_tx.py [sh600000,sz000001,...]

要换源就必须先对账（本站规矩）：`stock_daily` 只存**不复权价 + 真实涨跌幅**、
成交量单位是**股**、成交额单位是**元**。腾讯线若在单位或复权口径上不一致，
写进去就会把形态引擎的输入搞脏，而形态是拿 `pct_chg` 复利出前复权序列的。

本脚本做三件事：
1. 打印 akshare 腾讯接口的签名与原始返回（列名 / dtype / 末几行）
2. 同时拉 `adjust=""` 与 `adjust="qfq"`，看复权口径差在哪
3. 与本地 `stock_daily` 里**同一个 (code, trade_date)** 的行逐字段比 —— 库里的
   全市场日线是 iFinD 来的（`collect_kline`），拿它当标尺

在云端跑（验证可达性；脚本放在 ~ 下，所以后端目录用环境变量指）:
    scp scripts/probe_akshare_tx.py ubuntu@...:~/
    ssh ... "cd /opt/fupan && FUPAN_BACKEND=/opt/fupan/backend /opt/fupan/.venv/bin/python ~/probe_akshare_tx.py"
"""

import inspect
import os
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

import akshare as ak

codes = (sys.argv[1] if len(sys.argv) > 1 else "sz300030,sh600000").split(",")
START = (date.today() - timedelta(days=60)).strftime("%Y%m%d")
END = date.today().strftime("%Y%m%d")

print("akshare", getattr(ak, "__version__", "?"))
print("接口签名:", inspect.signature(ak.stock_zh_a_hist_tx))
print("区间:", START, "~", END)
print("代码:", codes)
print()

frames: dict[str, dict[str, object]] = {}

for code in codes:
    print("=" * 70)
    print(code)
    for label, adjust in (("不复权", ""), ("前复权", "qfq")):
        try:
            df = ak.stock_zh_a_hist_tx(
                symbol=code, start_date=START, end_date=END, adjust=adjust
            )
        except Exception:  # noqa: BLE001 - 探针要吞异常继续
            print(f"  [{label}] 失败：")
            traceback.print_exc()
            continue
        frames.setdefault(code, {})[label] = df
        print(f"  [{label}] 行数 {len(df)}  列 {list(df.columns)}")
        print(f"        dtype: {dict(df.dtypes.astype(str))}")
        if not df.empty:
            print("        末 2 行：")
            print(df.tail(2).to_string(index=False))
    print()

# ---------------------------------------------------------------- 与库对账
# 库里全市场日线是 iFinD 来的（`collect_kline`），拿它当标尺逐字段比
sys.path.insert(
    0,
    os.environ.get(
        "FUPAN_BACKEND", str(Path(__file__).resolve().parents[1] / "backend")
    ),
)
try:
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import StockDaily

    print("=" * 70)
    print("与本地 stock_daily（iFinD 口径）对账")
    for code in codes:
        plain = (frames.get(code) or {}).get("不复权")
        qfq = (frames.get(code) or {}).get("前复权")
        if plain is None or plain.empty:
            print(f"  {code}: 腾讯无数据，跳过")
            continue
        by_date = {str(r["date"])[:10]: r for r in plain.to_dict("records")}
        qfq_by_date = {str(r["date"])[:10]: r for r in qfq.to_dict("records")} if qfq is not None else {}
        with session_scope() as session:
            rows = session.scalars(
                select(StockDaily)
                .where(StockDaily.code == code[2:], StockDaily.trade_date.in_(set(by_date)))
                .order_by(StockDaily.trade_date)
            ).all()
        print(f"\n  {code}: 库里同区间 {len(rows)} 行 / 腾讯 {len(plain)} 行")
        if not rows:
            continue
        print(
            f"    腾讯 x 库 的比值（应全为 1 或同一定值）与字段差异统计："
        )
        mism: dict[str, int] = {}
        for r in rows:
            t = by_date[str(r.trade_date)]
            for f, tv in (
                ("open", t["open"]),
                ("high", t["high"]),
                ("low", t["low"]),
                ("close", t["close"]),
                ("volume", t["volume"]),
                ("amount", t["amount"]),
                ("turnover", t["turnover"]),
            ):
                dbv = getattr(r, f)
                if dbv in (None, 0) or tv in (None, 0):
                    continue
                if abs(float(dbv) / float(tv) - 1) > 1e-4:
                    mism[f] = mism.get(f, 0) + 1
        print(f"    iFinD vs 腾讯「不复权」不一致的行数（按字段）：{mism or '全部一致'}")

        # 不一致的字段到底差在哪：把最后两行并排打出来（单位差会表现为固定倍数）
        print("    末 2 行并排（库 / 腾讯）：")
        for r in rows[-2:]:
            t = by_date[str(r.trade_date)]
            print(
                f"      {r.trade_date}  "
                + "  ".join(
                    f"{f}={getattr(r, f)}/{t[f]}"
                    for f in ("open", "high", "low", "close", "volume", "amount", "turnover")
                )
            )

        # iFinD 的 `pct_chg` 是调整后的真实涨跌幅 → 与腾讯前复权序列算出来的比
        print(f"    {'日期':<12}{'库pct':>9}{'腾讯qfq推算':>12}{'库close':>10}{'腾讯close':>11}")
        prev_qfq: float | None = None
        pct_diffs = 0
        for r in rows:
            key = str(r.trade_date)
            q = qfq_by_date.get(key)
            calc = None
            if q is not None and prev_qfq:
                calc = (float(q["close"]) / prev_qfq - 1) * 100
            if q is not None:
                prev_qfq = float(q["close"])
            if calc is not None and r.pct_chg is not None:
                if abs(calc - float(r.pct_chg)) > 0.02:
                    pct_diffs += 1
            if r.trade_date >= rows[max(0, len(rows) - 4)].trade_date:
                print(
                    f"    {key:<12}{r.pct_chg!s:>9}"
                    f"{(f'{calc:.2f}' if calc is not None else '-'):>12}"
                    f"{r.close!s:>10}{t['close'] if (t := by_date.get(key)) else '-':>11}"
                )
        print(f"    pct_chg（库 vs 腾讯前复权推算）差 >0.02 的行：{pct_diffs}")
except Exception:  # noqa: BLE001 - 库里没有这张表 / 没装 sqlalchemy 都算正常
    print("（跳过对账：本机库不可用）")
    traceback.print_exc()
