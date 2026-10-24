"""指数估值数据源：蛋卷（danjuanfunds）公开的指数估值接口。

两个端点（**没有官方文档**，字段是我们按返回实测出来的）：

- `/djapi/index_eva/detail/{code}` —— 当前 PE/PB、分位、ROE、股息率（快照）
- `/djapi/index_eva/pe_history|pb_history/{code}?day=all` —— 历史序列，
  **周频**，实测 2016-10-24 起共 516 个点（正好近 10 年）

## 为什么是它

- 免 key、单次就回满 10 年，直接能算「10 年分位」；
- 中证官网（`ak.stock_zh_index_value_csindex`）虽然日频，但**一次只回最近 20 个交易日**，
  攒不出十年；
- 乐咕（`ak.stock_index_pe_lg`）覆盖的指数少（没有创业板指 / 科创50 / 中证红利），
  而且 2026-10-23 实测连续请求会被 **429**。

## 坑

未公开接口随时可能改字段。所以这里对每个字段逐个校验：拿不到就抛 `DanjuanError`，
由上层决定「这个指数这一轮跳过」—— **绝不写 0 / 空行冒充数据**。
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

BASE = "https://danjuanfunds.com/djapi/index_eva"
TIMEOUT = 20

# 不带 UA / Referer 时实测会被挡（403）；这两个值就是普通浏览器的值
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://danjuanfunds.com/",
}


class DanjuanError(RuntimeError):
    """取数失败（网络、非 200、result_code 非 0、字段缺失）。"""


#: 固定东八区（与 `app/timeutil.CST` 同口径，这里不 import 以免数据源依赖上层模块）
_CST = timezone(timedelta(hours=8))


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    """一个采样点。`day` 是采样日（周频，不是每个交易日）。"""

    day: date
    value: float


def _get(url: str) -> dict:
    try:
        response = requests.get(url, headers=_HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 —— 统一转成自己的异常
        raise DanjuanError(f"{url} 请求失败：{exc}") from exc

    # result_code 正常是 0；缺这个字段时看 data 是否存在（老版本没这层包装）
    code = payload.get("result_code")
    if code not in (0, None):
        raise DanjuanError(f"{url} result_code={code} msg={payload.get('msg')}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise DanjuanError(f"{url} 返回里没有 data")
    return data


def history(danjuan_code: str, kind: str) -> list[HistoryPoint]:
    """取 PE 或 PB 的历史采样点，按日期升序。

    `kind` 只接受 `pe` / `pb`：两个端点的返回结构一样，只有键名不同
    （`index_eva_pe_growths` / `index_eva_pb_growths`，值里的字段名同 `kind`）。
    """
    if kind not in ("pe", "pb"):
        raise ValueError(f"kind 只能是 pe / pb，收到 {kind}")
    data = _get(f"{BASE}/{kind}_history/{danjuan_code}?day=all")
    rows = data.get(f"index_eva_{kind}_growths")
    if not isinstance(rows, list):
        raise DanjuanError(f"{danjuan_code} 的 {kind} 历史里没有 growths 列表")

    points: list[HistoryPoint] = []
    for row in rows:
        ts = row.get("ts")
        value = row.get(kind)
        if not isinstance(ts, (int, float)) or value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        # ts 是毫秒时间戳。**按东八区取日期**（固定 +8，不依赖 tzdata）：
        # 实测 1477238400000 在 UTC 下是 2016-10-23 16:00 → 北京时间 10-24，
        # 用 UTC 会把整条序列的日期都提前一天
        day = datetime.fromtimestamp(ts / 1000, tz=_CST).date()
        points.append(HistoryPoint(day=day, value=value))

    if not points:
        raise DanjuanError(f"{danjuan_code} 的 {kind} 历史为空")
    points.sort(key=lambda point: point.day)
    return points
