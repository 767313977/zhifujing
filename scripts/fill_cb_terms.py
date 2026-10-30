"""把可转债的**条款**（票息 / 到期赎回价 / 到期日）一次性补齐。

为什么单独一个脚本：`bond_zh_cov_info` 是**一券一次请求**，全市场 400 多只要两分钟，
放进每天 17:30 的采集链会白占采集锁。每日采集只顺手补 `TERMS_PER_RUN` 只，
想让 YTM 立刻全部可用就跑这个。

用法（在 backend 目录下）：

    python ../scripts/fill_cb_terms.py            # 只补「今天还在交易、且没有票息」的券
    python ../scripts/fill_cb_terms.py --all      # 连已退市的一起补（回测用）
    python ../scripts/fill_cb_terms.py --limit 50 # 只补 50 只，控制耗时

限速是 1 请求/秒，311 只「在交易的」约 5 分钟；带 `--all` 会把 1000 多只全拉一遍，
十几分钟起步，仅首次建库 / 回测时用。
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select  # noqa: E402

from app.db import session_scope, upsert_many_fill  # noqa: E402
from app.jobs.collect_cb import _date, _num, _text  # noqa: E402
from app.models import CbBasic, CbDaily  # noqa: E402
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
    parser.add_argument(
        "--all", action="store_true", help="连已退市的一起补（默认只补最近一次快照里在交易的）"
    )
    parser.add_argument("--limit", type=int, default=0, help="最多补多少只（0 = 不限）")
    parser.add_argument("--codes", default="", help="只补这些代码，逗号分隔")
    args = parser.parse_args()

    with session_scope() as session:
        statement = select(CbBasic.bond_code).order_by(CbBasic.bond_code)
        statement = statement.where(CbBasic.coupon_text.is_(None))
        codes = list(session.scalars(statement))
        if not args.all:
            # 默认只补「最近一次快照里还在交易」的券：YTM 只对它们有意义，
            # 带 --all 会把 1000 多只已退市的也拉一遍（十几分钟）
            latest = session.scalar(
                select(CbDaily.trade_date).order_by(CbDaily.trade_date.desc())
            )
            alive = (
                set(session.scalars(select(CbDaily.bond_code).where(CbDaily.trade_date == latest)))
                if latest
                else set()
            )
            codes = [code for code in codes if code in alive]

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
