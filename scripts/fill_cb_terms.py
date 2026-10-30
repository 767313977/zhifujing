"""把可转债的**条款**（票息 / 到期赎回价 / 到期日）一次性补齐。

为什么单独一个脚本：`bond_zh_cov_info` 是**一券一次请求**，全市场 400 多只要两分钟，
放进每天 17:30 的采集链会白占采集锁。每日采集只顺手补 `TERMS_PER_RUN` 只，
想让 YTM 立刻全部可用就跑这个。

用法（在 backend 目录下）：

    python ../scripts/fill_cb_terms.py            # 只补还没有票息的
    python ../scripts/fill_cb_terms.py --all      # 全部重拉一遍（条款可能被改）
    python ../scripts/fill_cb_terms.py --limit 50 # 只补 50 只，控制耗时
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope, upsert_many_fill  # noqa: E402
from app.jobs.collect_cb import _date, _num, _text  # noqa: E402
from app.models import CbBasic  # noqa: E402
from app.sources.akshare_source import AkshareSource  # noqa: E402

logger = logging.getLogger("fill_cb_terms")


def fill(codes: list[str], source: AkshareSource) -> int:
    rows: list[dict] = []
    for index, code in enumerate(codes, start=1):
        info = source.cb_terms(code)
        if not info:
            continue
        conv_price = _num(info.get("TRANSFER_PRICE"))
        row: dict = {
            "bond_code": code,
            "coupon_text": _text(info.get("INTEREST_RATE_EXPLAIN")),
            "maturity_call_price": _num(info.get("EXECUTE_PRICE_SH")),
            "maturity_date": _date(info.get("EXPIRE_DATE")),
            "convert_start_date": _date(info.get("TRANSFER_START_DATE")),
        }
        redeem_price = _num(info.get("REDEEM_TRIG_PRICE"))
        if redeem_price and conv_price:
            row["redeem_trigger_pct"] = round(redeem_price / conv_price * 100, 2)
        resale_price = _num(info.get("RESALE_TRIG_PRICE"))
        if resale_price and conv_price:
            row["resale_trigger_pct"] = round(resale_price / conv_price * 100, 2)
        rows.append(row)
        if index % 50 == 0:
            logger.info("已取 %d/%d", index, len(codes))
    if not rows:
        return 0
    with session_scope() as session:
        return upsert_many_fill(session, CbBasic, rows)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="补齐可转债条款（票息 / 到期赎回价）")
    parser.add_argument("--all", action="store_true", help="全部重拉，不只是缺票息的")
    parser.add_argument("--limit", type=int, default=0, help="最多补多少只（0 = 不限）")
    parser.add_argument("--codes", default="", help="只补这些代码，逗号分隔")
    args = parser.parse_args()

    with session_scope() as session:
        statement = select(CbBasic.bond_code).order_by(CbBasic.bond_code)
        if not args.all:
            statement = statement.where(CbBasic.coupon_text.is_(None))
        codes = list(session.scalars(statement))

    if args.codes:
        wanted = {code.strip() for code in args.codes.split(",") if code.strip()}
        codes = [code for code in codes if code in wanted]
    if args.limit:
        codes = codes[: args.limit]

    if not codes:
        logger.info("没有需要补的券")
        return

    logger.info("待补 %d 只", len(codes))
    written = fill(codes, AkshareSource())
    logger.info("完成：写入 %d 只", written)


if __name__ == "__main__":
    main()
