"""通达信官网「每日盘后包」：某交易日全市场日线（含成交额）。

来源是通达信官网每天发布的增量包（单个 zip 约 2.7 MB），一次拿到沪深北**全部证券**
当日的昨收、开高低收、成交量（股）与成交额（元）。走 HTTP，和已失效的 mootdx TCP
行情命令不是一条路。

二进制布局参考 jing2uo/tdx2db（MIT）的 `tdx/merge.go`，2026-10-18 实测与本站
`stock_daily` 逐字段对拍一致：

| 代码 | 盘后包 close / volume / amount | 本站（iFinD 口径） |
| --- | --- | --- |
| 600519 | 1263.0 / 3,511,051 / 4,453,223,189 | 1263.0 / 3,511,051 / 4,453,223,190 |
| 000001 | 11.59 / 107,810,561 / 1,259,717,108.28 | 11.59 / 107,810,561 / 1,259,717,110 |
| 300750 | 297.75 / 54,152,671 / 16,028,123,330.46 | 297.75 / 54,152,671 / 16,028,123,300 |

⚠️ **包里不止股票**：指数、基金、债券都在（52k 行），代码还会跨市场重名
（`000001` 既是上证指数也是平安银行）。调用方必须按 `market + 代码` 一起匹配，
不能只按 6 位代码。

⚠️ **没有换手率**：本模块只给价格 / 量 / 额。`stock_daily.turnover` 仍要靠别处拿，
所以它目前只用于 ETF 的行情补全（`etf_share` 没有换手率这一列）。
"""

import io
import logging
import math
import re
import struct
import zipfile
from datetime import date

import requests

logger = logging.getLogger(__name__)

PACKAGE_URL = "https://www.tdx.com.cn/products/data/data/g4day/{ymd}.zip"

# 每个市场「有收盘价」的最少行数。2026-09-20 上游实测：沪 16202、深 3641（2022 年
# 以前深市大部分代码当日无价）、京 87。下限取明显低于最小值的整数。
MIN_PRICED = {"sh": 10000, "sz": 3000, "bj": 50}

# 盘后包从这一天起才带北交所文件（2026-09-20 二分实测：20220505 无、20220506 有）
BJ_FIRST_DAY = "20220506"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class TdxError(RuntimeError):
    """盘后包取不到或格式变了。"""


class TdxUnavailable(TdxError):
    """当天没有盘后包：非交易日、还没发布，或早于官网保留范围。"""


def _parse(content: bytes, ymd: str) -> list[dict]:
    """解析盘后包：每个市场一对 `.cod`（代码表，150 字节/条）+ `.md1`（行情块，512 字节/块）。"""
    archive = zipfile.ZipFile(io.BytesIO(content))
    names = set(archive.namelist())
    rows: list[dict] = []
    for market in ("sh", "sz", "bj"):
        cod_name, md1_name = f"{market}{ymd[2:]}.cod", f"{market}{ymd[2:]}.md1"
        if (
            market == "bj"
            and ymd < BJ_FIRST_DAY
            and cod_name not in names
            and md1_name not in names
        ):
            continue  # 这天之前的包还没有北交所文件；之后再缺就是格式变了
        if cod_name not in names or md1_name not in names:
            raise TdxError(f"盘后包缺少 {cod_name}/{md1_name}，格式可能已变")
        cod, md1 = archive.read(cod_name), archive.read(md1_name)
        if len(cod) % 150 or len(md1) % 512:
            raise TdxError(f"{market} 代码表或行情块长度不是整块，文件可能被截断")
        if len(cod) // 150 != len(md1) // 512:
            raise TdxError(
                f"{market} 代码表 {len(cod) // 150} 条、行情块 {len(md1) // 512} 块，对不上"
            )

        before, codes, seqs = len(rows), set(), set()
        for offset in range(0, len(cod), 150):
            record = cod[offset : offset + 150]
            code = record[0:6].rstrip(b"\x00 ").decode("ascii", "replace")
            seq = struct.unpack("<H", record[32:34])[0]
            if not re.fullmatch(r"[0-9]{6}", code):
                raise TdxError(f"盘后包 {market} 代码表出现非 6 位数字代码 {code!r}")
            if code in codes or seq in seqs:
                raise TdxError(f"盘后包 {market} 代码表有重复的代码 / 行情块序号（{code}, {seq}）")
            codes.add(code)
            seqs.add(seq)
            block = md1[seq * 512 : (seq + 1) * 512]
            if len(block) != 512:
                raise TdxError(f"{market}{code} 行情块越界（seq={seq}）")
            prev_close = struct.unpack("<d", block[4:12])[0]
            open_, high, low, close = struct.unpack("<4d", block[12:44])
            amount = struct.unpack("<d", block[72:80])[0]
            if not all(
                math.isfinite(v) for v in (prev_close, open_, high, low, close, amount)
            ):
                raise TdxError(f"盘后包 {market}{code} 出现非有限数值，文件可能损坏")
            if close <= 0:
                continue  # 880/881 等通达信自编板块指数整块为 0，不是证券
            volume = struct.unpack("<Q", block[56:64])[0]
            name = record[40:72].split(b"\x00")[0].decode("gbk", "replace").strip()
            if not name:
                raise TdxError(f"盘后包 {market}{code} 有价格却没有名称，文件可能损坏")
            rows.append(
                {
                    "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
                    "market": market,
                    "code": code,
                    "name": name,
                    "prev_close": round(prev_close, 4),
                    "open": round(open_, 4),
                    "high": round(high, 4),
                    "low": round(low, 4),
                    "close": round(close, 4),
                    "volume": volume,
                    "amount": round(amount, 2),
                }
            )
        if len(rows) - before < MIN_PRICED[market]:
            raise TdxError(
                f"盘后包 {market} 市场只有 {len(rows) - before} 条有价记录"
                f"（下限 {MIN_PRICED[market]}），文件可能残缺或格式已变"
            )
    return rows


def daily_package(day: date) -> list[dict]:
    """取某一交易日的全市场盘后包。

    非交易日、或当天包还没发布时抛 `TdxUnavailable`（不返回空表）；
    包损坏 / 格式变了抛 `TdxError`。
    """
    ymd = day.strftime("%Y%m%d")
    try:
        response = requests.get(
            PACKAGE_URL.format(ymd=ymd),
            headers={"User-Agent": _UA},
            timeout=(10, 120),
        )
    except requests.RequestException as exc:
        raise TdxError(f"下载盘后包失败: {type(exc).__name__}: {exc}") from exc
    if response.status_code == 404:
        raise TdxUnavailable(f"{day} 没有盘后包：非交易日、尚未发布，或超出官网保留范围")
    if response.status_code != 200:
        raise TdxError(f"盘后包返回 HTTP {response.status_code}")
    if not response.content.startswith(b"PK"):
        raise TdxError("盘后包不是 zip 文件，可能是错误页")
    try:
        return _parse(response.content, ymd)
    except (zipfile.BadZipFile, EOFError) as exc:
        raise TdxError(f"盘后包无法解压: {type(exc).__name__}: {exc}") from exc
