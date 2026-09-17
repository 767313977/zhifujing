"""探测 akshare 关键接口可用性。

akshare 接口变动频繁，换环境或升级后应重跑本脚本确认数据源是否可用。
用法: python scripts/probe_akshare.py [YYYYMMDD]
"""

import sys
import time
import traceback

import akshare as ak

DATE = sys.argv[1] if len(sys.argv) > 1 else "20260916"

RETRIES = 3
RETRY_WAIT = 2.0


def call_with_retry(fn):
    """东财接口存在偶发 RemoteDisconnected，需退避重试。"""
    last_exc = None
    for attempt in range(RETRIES):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 探测脚本需要吞掉所有异常继续跑
            last_exc = exc
            if attempt < RETRIES - 1:
                time.sleep(RETRY_WAIT * (attempt + 1))
    raise last_exc

CASES = [
    ("交易日历", lambda: ak.tool_trade_date_hist_sina()),
    ("全A实时快照", lambda: ak.stock_zh_a_spot_em()),
    ("行业板块", lambda: ak.stock_board_industry_name_em()),
    ("概念板块", lambda: ak.stock_board_concept_name_em()),
    ("涨停池", lambda: ak.stock_zt_pool_em(date=DATE)),
    ("跌停池", lambda: ak.stock_zt_pool_dtgc_em(date=DATE)),
    ("炸板池", lambda: ak.stock_zt_pool_zbgc_em(date=DATE)),
    ("个股资金流排名", lambda: ak.stock_individual_fund_flow_rank(indicator="今日")),
    ("行业板块资金流", lambda: ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")),
    ("龙虎榜", lambda: ak.stock_lhb_detail_em(start_date=DATE, end_date=DATE)),
    ("个股日线", lambda: ak.stock_zh_a_hist(symbol="000001", period="daily", start_date="20260801", end_date=DATE, adjust="qfq")),
]


def main() -> int:
    failed = 0
    for name, fn in CASES:
        started = time.monotonic()
        try:
            df = call_with_retry(fn)
            cols = ",".join(map(str, list(df.columns)[:10]))
            cost = time.monotonic() - started
            print(f"[OK]   {name:<12} rows={len(df):<6} {cost:5.1f}s cols={cols}")
        except Exception as exc:  # noqa: BLE001 - 探测脚本需要吞掉所有异常继续跑
            failed += 1
            print(f"[FAIL] {name:<12} {type(exc).__name__}: {str(exc)[:120]}")
            if "--trace" in sys.argv:
                traceback.print_exc()
        time.sleep(1.0)
    print(f"\n合计 {len(CASES)} 个接口, 失败 {failed} 个")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
