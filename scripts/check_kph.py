"""开盘红数据服务（`StockDataServer.exe`）的**只读探针**。

用途：在决定「接哪几块」之前，把《开盘红接口说明书 v2.7.2》里与本站相关的接口
真机跑一遍，输出一份**实测字段对照**（`docs/开盘红接口实测_<日期>.md`）。

设计上的几条硬约束：

1. **只读**：不调 `/api/kph/login`、不调 `/api/send_hex`、不改任何东西；
2. **不制造订阅**：`get_market_sentiment` / `get_realtime_lhb` / `get_kph_trends*` 都是
   订阅型（调用后服务端会持续推送），默认**一律不碰**，改用文档推荐的一次性版本
   （`_once` / `_range`）。确实要看订阅型，加 `--with-subscribe`，且脚本会自己退订；
3. **零依赖**：只用标准库 `urllib`（服务端是本机 HTTP，不需要 `requests`/`httpx`）；
4. 服务没起时给出人话提示并以退出码 2 结束 —— 别让人以为脚本坏了。

用法：
    python scripts/check_kph.py                     # 跑默认这组接口，写 docs/
    python scripts/check_kph.py --only limit_up,zd_stat
    python scripts/check_kph.py --base http://127.0.0.1:5000 --out docs/xxx.md
    python scripts/check_kph.py --dump-dir .tmp/kph  # 另存每个接口的原始 JSON
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:5000"


@dataclass
class Call:
    """一个要实测的接口。"""

    key: str
    path: str
    params: dict = field(default_factory=dict)
    #: 站内可以对接的表 / 页面（写在报告里，供决定「接哪块」时参考）
    target: str = ""
    #: 特别说明（单位、坑、一次性 vs 订阅）
    note: str = ""
    #: 订阅型：默认不调（见文件头第 2 条）
    subscribe: bool = False


# 顺序即报告顺序：先大盘 / 情绪，再池子 / 榜，再板块，最后个股与代理
CALLS: tuple[Call, ...] = (
    Call(
        "zd_stat",
        "/api/get_zd_stat",
        target="`MarketSentiment`（涨跌家数、涨跌停家数）/ 首页情绪面板",
        note="`zdList` 是 -10…10 档位的家数分布，可对应 `BreadthDaily` 的档位统计",
    ),
    Call(
        "sentiment_once",
        "/api/get_market_sentiment_once",
        {"type": 0},
        target="`MarketSentiment`（情绪分、涨停数、连板高度）",
        note="**一次性版**（订阅 → 取一帧 → 退订）；订阅版 `get_market_sentiment` 默认不调",
    ),
    Call(
        "daban_head",
        "/api/get_daban_head",
        target="情绪面板（今日/昨日涨停数、封板率）",
        note="`todayFB` = 封板率（%）",
    ),
    Call(
        "daban_count",
        "/api/get_daban_list_count",
        target="打板页各 Tab 条数（可与 `LimitPool` 行数对账）",
    ),
    Call(
        "zt_situation",
        "/api/get_zt_situation",
        target="涨停梯队（连板高度分布）",
    ),
    Call(
        "market_energy",
        "/api/get_market_energy",
        target="市场能量（成交额）/ 首页",
    ),
    Call(
        "weight_performance",
        "/api/get_weight_performance",
        target="权重股表现 / 首页",
    ),
    Call(
        "northbound",
        "/api/get_northbound_funds",
        target="`HsgtDaily`（北向资金）",
    ),
    Call(
        "limit_up",
        "/api/get_limit_up_stock_list",
        {"order": 1, "count": 3},
        target="`LimitPool`（pool_type=zt）",
        note="`list[].1`=代码、`2`=名称、`5`=连板标记、`100`=原始列数组",
    ),
    Call(
        "limit_down",
        "/api/get_limit_down_stock_list",
        {"order": 1, "count": 3},
        target="`LimitPool`（pool_type=dt）",
    ),
    Call(
        "broken",
        "/api/get_broken_stock_list",
        {"order": 1, "count": 3},
        target="`LimitPool`（炸板）",
    ),
    Call(
        "natural_limit",
        "/api/get_natural_limit_stock_list",
        {"order": 1, "count": 3},
        target="`LimitPool`（自然涨停）",
    ),
    Call(
        "once_limit_down",
        "/api/get_once_limit_down_stock_list",
        {"order": 1, "count": 3},
        target="`LimitPool`（曾跌停）",
    ),
    Call(
        "new_stock",
        "/api/get_new_stock_stock_list",
        {"order": 1, "count": 3},
        target="新股池（站内暂无表）",
    ),
    Call(
        "auction",
        "/api/get_auction_stock_list",
        {"order": 1, "count": 3},
        target="`AuctionSnapshot`（竞价）",
    ),
    Call(
        "market_radar",
        "/api/get_market_radar",
        target="异动雷达 / 首页（站内暂无表）",
    ),
    Call(
        "lhb_range",
        "/api/get_realtime_lhb_range",
        {"start_time": 93000, "end_time": 150000, "count": 3},
        target="`Lhb` / `LhbInstitution`",
        note="**区间一次性版**；订阅版 `get_realtime_lhb` 默认不调",
    ),
    Call(
        "forecast_lhb",
        "/api/get_forecast_lhb",
        {"count": 3},
        target="龙虎榜 · 要涨吗（站内暂无表）",
    ),
    Call(
        "plate_quotas",
        "/api/get_plate_type_quotas",
        {"plate_type": 4, "count": 5},
        target="`SectorDaily` / `SectorBasic`",
        note="`plate_type` 只能 4(行业) / 6(地区) / 7(精选)，别的值直接 500",
    ),
    Call(
        "hot_block",
        "/api/get_hot_block_list",
        {"st": 5},
        target="热门板块（站内暂无表）",
        note="每页条数是 **`st`** 不是 `count`（`count=2` 仍返回 50 条）",
    ),
    Call(
        "block_ex",
        "/api/get_block_stock_list_ex",
        {"st": 5},
        target="`SectorMember`（板块成分股）",
        note="`bk_code` 缺省时脚本用 `plate_quotas` 返回的第一个 plateId 补上",
    ),
    Call(
        "index_quotas",
        "/api/get_index_simple_quotas",
        {"stock_ids": "SH000001,SZ399001,SZ399006"},
        target="`IndexDaily`（指数简版行情）",
        note="`price` / `incPrice` **÷10000** 才是点位",
    ),
    Call(
        "main_index_trends",
        "/api/get_main_index_trends",
        target="指数分时（首页 / 指数页）",
        note="`points[].price` ÷10000",
    ),
    Call(
        "stock_pk",
        "/api/get_stock_pk",
        {"stock_id": "600519"},
        target="个股快照 + 盘口（个股页）",
        note="`price` / `avg` / `high` / `low` ÷10000",
    ),
    Call(
        "stock_mx",
        "/api/get_stock_mx",
        {"stock_id": "600519"},
        target="逐笔成交明细（个股页）",
        note="`price` ÷10000",
    ),
    Call(
        "related_theme",
        "/api/get_stock_related_theme",
        {"stock_id": "600519"},
        target="`StockConcept`（个股关联主题）",
    ),
    Call(
        "related_plate",
        "/api/get_stock_related_plate",
        {"stock_id": "600519"},
        target="个股关联板块（个股页）",
    ),
    Call(
        "today_zt_reason",
        "/api/get_today_zt_reason",
        {"code": "600519"},
        target="`LimitReason`（今日涨停原因）",
        note="参数是 `code`（纯 6 位），不是 `stock_id`",
    ),
    Call(
        "message_express",
        "/api/get_message_express",
        {"code": "600519"},
        target="个股消息快报（个股页 / 简报）",
    ),
    Call(
        "pop_rank",
        "/api/get_stock_pop_rank",
        {"type_": 1, "count": 3},
        target="开盘红人气榜（站内暂无表）",
        note="排序字段是 **`type_`**（带下划线）；非法值不报错、直接回默认 50 条",
    ),
    Call(
        "abnormal_price",
        "/api/get_abnormal_price",
        {"stock_id": "SH600519"},
        target="异动预警线（个股页）",
        note="这里要带市场前缀（`SH`/`SZ`），与其它个股接口不同",
    ),
    Call(
        "quotes",
        "/api/get_stock_quotes",
        {"secids": "1.600519,0.000001"},
        target="`StockDaily` 当日快照校验（实时行情）",
        note="腾讯/东财代理，**无外层包装**；`secids` 是东财格式（1.=沪 0.=深）",
    ),
    Call(
        "kline",
        "/api/get_stock_kline",
        {"code": "600519", "daynum": 5, "source": "tencent"},
        target="`StockDaily`（日 K 交叉校验）",
        note="`klines` 是逗号分隔字符串数组",
    ),
    Call(
        "tradeday",
        "/api/get_tradeday_list",
        {"daynum": 5},
        target="`TradeCalendar`（交易日历）",
    ),
    # ---- 订阅型：默认不跑，`--with-subscribe` 才会碰（脚本会自己退订）----
    Call(
        "kph_trends",
        "/api/get_kph_trends",
        {"code": "600519", "timeout": 5},
        target="个股分时（个股页，可替代/校验东财分时）",
        note="**订阅型**，跑完脚本会调 `/api/unsubscribe_kph`",
        subscribe=True,
    ),
)


def _get(base: str, call: Call, timeout: float) -> dict:
    """GET 一个接口，返回 {ok, status, ms, json|error}。"""
    query = urllib.parse.urlencode(call.params)
    url = f"{base}{call.path}" + (f"?{query}" if query else "")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也可能带 JSON 说明
        raw = exc.read()
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "status": None,
            "ms": int((time.perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }
    elapsed = int((time.perf_counter() - started) * 1000)
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        return {
            "ok": False,
            "status": status,
            "ms": elapsed,
            "error": f"不是合法 JSON（{exc}）",
            "text": raw[:300].decode("utf-8", "replace"),
        }
    return {
        "ok": 200 <= status < 300 and payload.get("success") is not False,
        "status": status,
        "ms": elapsed,
        "json": payload,
        "url": url,
    }


def _shape(value, depth: int = 0) -> str:
    """把一个 JSON 值压成一行结构描述：`dict{a,b}` / `list[3] of dict{x,y}`。"""
    if isinstance(value, dict):
        keys = ", ".join(list(value)[:12])
        more = "…" if len(value) > 12 else ""
        return f"dict{{{keys}{more}}}"
    if isinstance(value, list):
        if not value:
            return "list[0]"
        return f"list[{len(value)}] of {_shape(value[0], depth + 1)}"
    if value is None:
        return "null"
    return type(value).__name__


def _field_rows(payload) -> list[tuple[str, str, str]]:
    """从响应里尽量抽出「字段 / 类型 / 样例」三列。

    开盘红 mTLS 类接口的业务数据在 `response.data`；代理类（腾讯/东财）没有外层
    包装，直接把顶层当数据。列表类接口只取第一行来说字段（报告里会注明）。
    """
    data = payload
    if isinstance(payload.get("response"), dict):
        data = payload["response"].get("data", {})
    if not isinstance(data, dict):
        return []
    rows: list[tuple[str, str, str]] = []
    for key, value in data.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            for inner, cell in list(value[0].items())[:10]:
                sample = json.dumps(cell, ensure_ascii=False)
                rows.append((f"{key}[0].{inner}", _shape(cell), sample[:60]))
        elif isinstance(value, dict):
            for inner, cell in list(value.items())[:10]:
                sample = json.dumps(cell, ensure_ascii=False)
                rows.append((f"{key}.{inner}", _shape(cell), sample[:60]))
        else:
            sample = json.dumps(value, ensure_ascii=False)
            rows.append((key, _shape(value), sample[:60]))
    return rows


def _cut(text: str, limit: int = 1600) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…（截断）"


def _status_text(result: dict) -> str:
    if result.get("status") is None:
        return "不可达"
    return f"{result['status']}"


def main() -> int:
    parser = argparse.ArgumentParser(description="开盘红数据服务只读探针")
    parser.add_argument("--base", default=DEFAULT_BASE, help="服务地址（默认本机 5000）")
    parser.add_argument("--timeout", type=float, default=20.0, help="单接口超时秒数")
    parser.add_argument("--only", default="", help="只跑这些 key，逗号分隔")
    parser.add_argument("--with-subscribe", action="store_true", help="连订阅型接口也跑")
    parser.add_argument("--dump-dir", default="", help="把每个接口的原始 JSON 写在这个目录")
    parser.add_argument("--out", default="", help="报告路径（默认 docs/开盘红接口实测_<日期>.md）")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    wanted = {item.strip() for item in args.only.split(",") if item.strip()}
    calls = [
        call for call in CALLS if (not wanted or call.key in wanted)
        and (args.with_subscribe or not call.subscribe)
    ]

    # 先看服务活着没有：连不上就没必要逐个超时 20 秒
    health = _get(base, Call("status", "/api/status"), timeout=min(args.timeout, 5))
    if health["status"] is None:
        print(f"✗ 连不上 {base}：{health['error']}")
        print("  先把 StockDataServer.exe 跑起来，等控制台出现「远程服务器连接成功！」再试。")
        return 2
    state = health.get("json", {})
    print(f"✓ 服务在线：connected={state.get('connected')} initialized={state.get('initialized')}")

    results: list[dict] = []
    plate_id = ""
    raw_dump = Path(args.dump_dir) if args.dump_dir else None
    if raw_dump:
        raw_dump.mkdir(parents=True, exist_ok=True)

    for call in calls:
        # `block_ex` 的 bk_code 要拿板块列表里的第一个（脚本内自动补，免得手填）
        if call.key == "block_ex" and plate_id:
            call.params.setdefault("bk_code", plate_id)
        result = _get(base, call, args.timeout)
        result["call"] = call
        results.append(result)
        mark = "✓" if result["ok"] else "✗"
        detail = "" if result["ok"] else f"  {result.get('error', '')}"
        print(f"{mark} {call.key:<18} {_status_text(result):>4}  {result['ms']:>5} ms{detail}")
        if raw_dump is not None:
            (raw_dump / f"{call.key}.json").write_text(
                json.dumps(result.get("json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if call.key == "plate_quotas" and result.get("json"):
            items = (result["json"].get("response") or {}).get("data", {}).get("items") or []
            if items and isinstance(items[0], dict):
                plate_id = str(items[0].get("plateId") or items[0].get("plate_id") or "")

    # 订阅型接口用完就退订（避免把服务端订阅名额占着）
    if any(result["call"].subscribe for result in results):
        clearing = _get(base, Call("unsub", "/api/unsubscribe_kph_all"), args.timeout)
        print(f"{'✓' if clearing['ok'] else '✗'} 已退订全部分时订阅")

    out = Path(args.out) if args.out else Path("docs") / f"开盘红接口实测_{date.today():%Y%m%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render(base, health, results), encoding="utf-8")
    print(f"\n报告：{out}")

    failed = [result for result in results if not result["ok"]]
    if failed:
        print(f"有 {len(failed)} 个接口没跑通：{', '.join(r['call'].key for r in failed)}")
        return 1
    return 0


def _render(base: str, health: dict, results: list[dict]) -> str:
    state = health.get("json", {})
    lines = [
        f"# 开盘红数据服务 · 接口实测与字段对照（{date.today():%Y-%m-%d}）",
        "",
        f"- 服务地址：`{base}`",
        f"- `/api/status`：`connected={state.get('connected')}`、"
        f"`initialized={state.get('initialized')}`、`{state.get('timestamp', '')}`",
        "- 探针：`scripts/check_kph.py`（只读；订阅型接口默认跳过，跑完会退订）",
        f"- 实测时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        "> 数值口径见说明书 §2.4：开盘红协议里价格 / 金额多为 ×10000 的整数，"
        "下面「字段」列会标出需要 ÷10000 的那些。",
        "",
        "## 总览",
        "",
        "| 接口 | 结果 | HTTP | 耗时 | 顶层结构 | 站内可对接 |",
        "|---|---|---|---|---|---|",
    ]
    for result in results:
        call = result["call"]
        payload = result.get("json") or {}
        top = _shape(payload.get("response", payload)) if payload else "—"
        lines.append(
            f"| `{call.path}` | {'✓' if result['ok'] else '✗'} | "
            f"{_status_text(result)} | {result['ms']} ms | `{top}` | {call.target} |"
        )

    for result in results:
        call = result["call"]
        lines += ["", f"## `{call.path}`", ""]
        if call.params:
            lines.append(f"- 请求：`GET {call.path}?{urllib.parse.urlencode(call.params)}`")
        if call.note:
            lines.append(f"- 说明：{call.note}")
        if call.target:
            lines.append(f"- 站内可对接：{call.target}")
        payload = result.get("json") or {}
        if isinstance(payload.get("response"), dict):
            lines.append(f"- cmd：`{payload['response'].get('cmd_id', '')}`")
        lines += ["", f"- 结果：{'✓' if result['ok'] else '✗'}（{_status_text(result)}，{result['ms']} ms）"]
        if not result["ok"] and result.get("error"):
            lines += ["", f"```\n{result['error']}\n```"]
            continue
        rows = _field_rows(payload)
        if rows:
            lines += ["", "| 字段 | 类型 | 样例 |", "|---|---|---|"]
            lines += [f"| `{path}` | {kind} | `{sample}` |" for path, kind, sample in rows]
        else:
            lines += ["", "（没有可展开的字段：响应不是 dict 结构）"]
        lines += ["", "```json", _cut(json.dumps(payload, ensure_ascii=False, indent=2)), "```"]

    lines += [
        "",
        "## 下一步怎么读这份报告",
        "",
        "1. 「总览」里 ✗ 的先看是不是服务端的 mTLS 断了（说明书 §7.2：某接口超时后整条链路失效，"
        "要重启 exe）；",
        "2. 字段列里带 `100` 这种数字键的（打板池），是上游的**原始列数组**，顺序要按说明书或"
        "上游 proto 对齐 —— 直接对接时优先用 `1`(代码) / `2`(名称) / `5`(连板) 这些有名字的键；",
        "3. 要接哪几块，按「站内可对接」那列挑，再按现有 `sources/` + `jobs/` 的写法接。",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
