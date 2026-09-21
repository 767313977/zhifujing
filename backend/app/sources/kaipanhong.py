"""开盘红板块数据源。

开盘红是**开盘啦团队的新版 App**（开发者 深圳银之杰拓扑技术有限公司，运营者
深圳开盘啦网络科技有限公司）。它的「精选板块」是自有分类（270 个），命名贴近
短线复盘的语言：芯片 / 算力 / AI应用 / 机器人概念 / 次新股。本项目用它替换原先
的同花顺口径，见设计文档 8.32。

两个域名分工明确，**不是同一个接口的两个地址**：

- `apphwshhq`：当日实时（`Date` 给当天）
- `apphis`：历史（`Date` 给历史日期）

鉴权：`Token=0` / `UserID=0` 的**游客态就够了**，不需要账号。历史接口也不需要 ——
这点与网上流传的「历史必须登录 Token」相反，实测 w45 版接口不要。

⚠️ 返回的是**位置数组**（19 列、没有字段名），而且不同 ZSType 的列语义还不一样。
本项目自己反解（见下面的列下标），**刻意不引第三方 SDK**：实测 levistock 把 `[17]`
标成「成分股数量」，而在精选口径里它等于 `[2]`、在行业口径里等于 `[3]` ——
照抄它的映射会把两个毫无关系的数当成同一件事。
"""

import logging
from datetime import date

import requests

from app.config import Settings, get_settings
from app.sources.base import TokenBucket, retry_call

logger = logging.getLogger(__name__)

# --- 口径 ---
# 值不能超过 16 字符：数据库里 sector_daily.taxonomy 是 String(16)
TAXONOMY_SELECTED = "kph_selected"
TAXONOMY_INDUSTRY = "kph_industry"
TAXONOMIES = (TAXONOMY_SELECTED, TAXONOMY_INDUSTRY)

# ZSType：开盘红自己的板块体系编号。地区(6) 复盘用不上，没接。
ZS_TYPE = {
    TAXONOMY_SELECTED: "7",
    TAXONOMY_INDUSTRY: "4",
}

# --- 板块行的列下标 ---
#
# 已「对上账」的列。反解方法不是猜，而是：把该板块的成分股逐只求和，再与板块行
# 逐列比对，相等的那一列就是它（见设计文档 8.32.2 的三组实测数据）：
#   海峡两岸（地区，303 只）[5]=164,667,094,504 vs 求和 164,667,094,468 → 1.000000
#   半导体  （行业，182 只）[5]=331,964,637,506 vs 求和 331,964,637,437 → 1.000000
#   [18] 恒等于 [3]，是同一个值的重复列
COL_CODE = 0
COL_NAME = 1
# `[2]` = **强度值**，开盘啦自己的合成指标（文档口径：「强度值上万即代表板块处于强势
# 状态」）。它才是开盘啦 App 板块榜的排序依据 —— 按成交额排得到的是另一张榜。
#
# 两条实测依据（2026-09-21）：
# 1. 按 `[2]` 降序取前 5，与开盘啦 App 当天那一列**逐个对上**：
#    医药 12646 / 地产链 7272 / AI应用 6189 / 芯片 5416 / 化工 5217
# 2. 两个口径**都是强度**，只是量纲差得远（精选 -334~12646、中位数 180；
#    行业 -144~1083、中位数 224）—— 精选板块是题材集合、弹性大，行业是宽口径。
#    **所以强度可以跨日比、不能跨口径比。**
#
# ⚠️ 这里原先写的是「`[2]` 一律不取」：理由是「精选里是强度、行业里是别的数，取它会
# 串味」。那个判断是错的 —— 我只看到「同一列在两个口径下数量级差很多」，就当成两种
# 含义，没去和开盘啦的榜对一遍。**数量级不同不等于语义不同。**
COL_STRENGTH = 2
COL_PCT_CHG = 3
COL_AMOUNT = 5

# `[5]` 在**三个口径上都对上账**（下表是 2026-09-18 实测；成分股去重后求和与板块行
# 的差都在 220 元以内，属于浮点取整级别的误差）：
#
#   海峡两岸（地区，303 只）  板块行 164,667,094,504 vs 求和 164,667,094,468
#   半导体  （行业，182 只）  板块行 331,964,637,506 vs 求和 331,964,637,437
#   芯片    （精选，1130 只） 板块行 1,014,182,994,691 vs 求和 1,014,182,994,472
#
# ⚠️ 这里曾经有个**假的**结论：精选口径对不上账（1.134 倍）。真相是成分股分页
# 越界会重复吐尾部（见 MAX_PAGES 上面的对照表），多出的重复行把求和灌偏了 ——
# 修掉分页之后三个口径一模一样地精确。教训：**对不上账时先怀疑取数，别急着
# 给数据源记一笔「口径不一致」**。

# 以下列**一律不取**，哪怕看起来像需要的字段：
#
#   [17] 精选 = `[2]`（强度）的重复，行业 = `[3]`（涨跌幅）的重复，
#        不能当「成分股数量」用。
#   [6][7][8][12] 看起来像净流入 / 主买 / 主卖，但拿成分股逐只求和一条都对不上
#        （板块级口径与成分股求和不一致），**没有验证手段就不填**。
#   [15][16] 在各口径下占比之和 71%~113% 不等，不是涨跌家数占比。
#
# 净流入、涨跌家数、领涨股、成分股数量因此全部留空。宁可空着让页面显示 `—`，
# 也不填一个猜的数 —— 与 services/sentiment.py 里「不能拿 0 顶替缺失」同一个取舍。

# 成分股行的列下标。反解同样靠对账：成员行 [7] 逐只求和能精确对上板块行的 [5]。
MEMBER_CODE = 0
MEMBER_NAME = 1
MEMBER_TAGS = 4
MEMBER_PRICE = 5
MEMBER_PCT_CHG = 6
MEMBER_AMOUNT = 7
MEMBER_TURNOVER = 8
MEMBER_FLOAT_MV = 10

# 涨停天梯的列下标。这是**个股 → 板块**的唯一来源（非涨停个股没有可用接口，
# 见设计文档 8.32.4），所以它同时也决定了涨停题材聚类能不能做。
LADDER_CODE = 0
LADDER_NAME = 1
LADDER_CONSECUTIVE = 2
LADDER_SEAL_TIME = 3
LADDER_BOARD_CODE = 4
LADDER_BOARD_NAME = 5
LADDER_BOARD_LIMIT_COUNT = 8
LADDER_AMOUNT = 9

_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    # 必须装成安卓客户端：默认的 python-requests UA 会被拒
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; 2206123SC Build/c069a49.2)",
    "Accept-Encoding": "gzip",
}

_BASE_PARAMS = {
    "PhoneOSNew": "1",
    # 固定值即可，不需要真的去注册一台设备
    "DeviceID": "1a609dd6-b2b8-3bf9-ac40-a77581551454",
    "VerSion": "6.0.6",
    "Token": "0",
    "UserID": "0",
    "Red": "1",
    "apiv": "w45",
}

HOST_REALTIME = "https://apphwshhq.kaipanhong.com/w1/api/index.php"
HOST_HISTORY = "https://apphis.kaipanhong.com/w1/api/index.php"

# 板块排行每页条数。50 是接口本身用的档位，别乱改 —— 改小只会多花请求。
PAGE_SIZE = 50
# 成分股每页条数。实测一次能给 1000 行，芯片板块（1130 只）两页拿完。
MEMBER_PAGE_SIZE = 1000
# 翻页上限，只用来防死循环。
MAX_PAGES = 40

# ⚠️ 两个接口的**翻页收尾行为不一样**，不能共用一套停法：
#
# | 接口 | 越界请求 | 停法 |
# | --- | --- | --- |
# | `RealRankingInfo` 板块排行 | 返回**空页** | 短页即可停 |
# | `ZhiShuStockList_W8` 成分股 | **重复吐尾部**（实测要 Index=2000 时又给了 260 行，
#   且那 260 行的代码全部与前面重复） | **只能靠短页停**，绝不能靠空页 |
#
# 成分股那次踩出来的具体后果：芯片板块去重后是 1130 只，但按「翻到空页为止」会拿到
# 1390 行、多出 260 个重复代码，成交额求和从 8146 亿被灌到 12137 亿 ——
# 行数看着正常、日期也对，只有去重才发现。所以两个接口用**各自的**短页判据。
#
# 另外注意板块排行历史接口的 `Count` 只报**当页**条数（见下面 board_ranking），
# 而成分股接口的 `Count` 是真总数（1130），可以拿来对账。


def _to_float(value: object) -> float | None:
    """接口里的数字有时是字符串、有时是空串，统一成 float 或 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


class KaipanhongSource:
    """开盘红板块与涨停天梯数据源。线程安全（限速器有锁保护）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._bucket = TokenBucket(self.settings.kph_rate_limit)

    def _post(self, trade_date: date, params: dict, description: str) -> dict:
        """POST 一次。按日期自动选实时/历史域名，并把 errcode 当异常抛出。"""
        host = HOST_REALTIME if trade_date == date.today() else HOST_HISTORY
        body = {**_BASE_PARAMS, "Date": trade_date.isoformat(), **params}

        def _do() -> dict:
            self._bucket.acquire()
            response = requests.post(
                host, headers=_HEADERS, data=body, timeout=self.settings.http_timeout
            )
            response.raise_for_status()
            return response.json()

        payload = retry_call(
            _do,
            retries=self.settings.http_retries,
            backoff=self.settings.http_backoff,
            description=description,
        )
        # errcode=0 才是成功。注意**空数据也是 errcode=0**（换个接口名就是空 List），
        # 所以调用方不能只看这一层，必须校验行数。
        if str(payload.get("errcode")) != "0":
            raise RuntimeError(f"{description} 返回 errcode={payload.get('errcode')}")
        return payload

    # ------------------------------------------------------------ 板块排行

    def board_ranking(self, trade_date: date, taxonomy: str) -> list[dict]:
        """某个口径下的板块排行（精选 270 / 行业 104）。

        停法是**短页**（返回条数 < PAGE_SIZE）。不要拿 `Count` 当总数：
        历史接口的 `Count` 只报当页条数（一页 50 就是 50、最后一页 20 就是 20），
        只有实时接口的 `Count` 才是真总数（270）。按 Count 截会静默只写 50 个板块。

        返回的每一项带四个已确认的字段：`sector_code` / `name` / `strength`（开盘啦
        强度值，也是它 App 里板块榜的排序依据）/ `pct_chg` / `amount`。
        其余字段为什么不敢取，见模块顶部。
        """
        zs_type = ZS_TYPE.get(taxonomy)
        if zs_type is None:
            raise ValueError(f"未知口径 {taxonomy}，只能是 {list(ZS_TYPE)}")

        rows: list[dict] = []
        for page in range(MAX_PAGES):
            payload = self._post(
                trade_date,
                {
                    "a": "RealRankingInfo",
                    "c": "ZhiShuRanking",
                    "Order": "1",
                    "Type": "1",
                    "ZSType": zs_type,
                    "st": str(PAGE_SIZE),
                    "Index": str(page * PAGE_SIZE),
                },
                f"开盘红板块排行 {taxonomy} {trade_date} 第 {page + 1} 页",
            )
            batch = payload.get("list") or []
            rows.extend(self._parse_board(row) for row in batch)
            if len(batch) < PAGE_SIZE:
                break
        else:
            logger.warning(
                "开盘红板块排行 %s %s 翻到 %d 页仍未结束，可能有板块被漏掉",
                taxonomy,
                trade_date,
                MAX_PAGES,
            )
        return rows

    @staticmethod
    def _parse_board(row: list) -> dict:
        def cell(index: int) -> object:
            return row[index] if index < len(row) else None

        return {
            "sector_code": str(cell(COL_CODE) or "").strip(),
            "name": str(cell(COL_NAME) or "").strip(),
            "strength": _to_float(cell(COL_STRENGTH)),
            "pct_chg": _to_float(cell(COL_PCT_CHG)),
            "amount": _to_float(cell(COL_AMOUNT)),
        }

    # ------------------------------------------------------------ 成分股

    def board_members(self, plate_id: str, trade_date: date) -> list[dict]:
        """某个板块在**历史某日**的成分股。

        ⚠️ 停法只能用**短页**，绝不能「翻到空页为止」：这个接口越界请求不是返回空页，
        而是**重复吐尾部**（实测 Index=2000 时又给 260 行，代码全部与前面重复）。
        按空页停会把芯片板块灌成 1390 行、多出 260 个重复代码 —— 见模块顶部的对照表。

        ⚠️ 当日数据的**可用时间不确定**：同一天 21:20 请求 `Date=今天` 返回
        `errcode=1020`，21:55 再请求就有数据了 —— 它是在盘后某个时刻才更新出来的、
        不是固定时刻。所以调用方要把「取不到」当成正常分支处理（当日早看就没有），
        而不是显示成「该板块没有成分股」。

        比原先的 iFinD 选股接口好的一点是它不分页上限截断（一次 1000 行、翻页到底），
        上千只成分股的大板块不会被静默砍成 100 只。
        """
        members: list[dict] = []
        reported: int | None = None
        for page in range(MAX_PAGES):
            payload = self._post(
                trade_date,
                {
                    "a": "ZhiShuStockList_W8",
                    "c": "ZhiShuRanking",
                    "Order": "1",
                    "Type": "6",
                    "PlateID": plate_id,
                    "old": "1",
                    "IsZZ": "0",
                    "IsKZZType": "0",
                    "TSZB": "0",
                    "TSZB_Type": "0",
                    "filterType": "0",
                    "st": str(MEMBER_PAGE_SIZE),
                    "Index": str(page * MEMBER_PAGE_SIZE),
                },
                f"开盘红板块成分股 {plate_id} {trade_date} 第 {page + 1} 页",
            )
            if reported is None:
                # 这个接口的 Count 是**真总数**（与板块排行不同），可以拿来对账
                reported = payload.get("Count")
            batch = payload.get("list") or []
            members.extend(self._parse_member(row) for row in batch)
            if len(batch) < MEMBER_PAGE_SIZE:
                break
        else:
            logger.warning(
                "开盘红成分股 %s %s 翻到 %d 页仍未结束，可能有成分股被漏掉",
                plate_id,
                trade_date,
                MAX_PAGES,
            )

        if isinstance(reported, int) and reported != len(members):
            logger.warning(
                "开盘红成分股 %s %s 对不上账：接口说 %d 只，实际取到 %d 只",
                plate_id,
                trade_date,
                reported,
                len(members),
            )
        return members

    @staticmethod
    def _parse_member(row: list) -> dict:
        def cell(index: int) -> object:
            return row[index] if index < len(row) else None

        return {
            "code": str(cell(MEMBER_CODE) or "").strip(),
            "name": str(cell(MEMBER_NAME) or "").strip(),
            "price": _to_float(cell(MEMBER_PRICE)),
            "pct_chg": _to_float(cell(MEMBER_PCT_CHG)),
            "amount": _to_float(cell(MEMBER_AMOUNT)),
            "turnover": _to_float(cell(MEMBER_TURNOVER)),
            "float_mv": _to_float(cell(MEMBER_FLOAT_MV)),
            # 个股的概念标签（如「医药零售、AI应用」）。**只是标签文本**，不能拿来
            # 当板块归属用：它是开盘红的分类词，与本项目的板块代码对不上。
            "tags": str(cell(MEMBER_TAGS) or "").strip(),
        }

    # ------------------------------------------------------------ 涨停天梯

    def limit_up_ladder(self, trade_date: date) -> list[dict]:
        """涨停天梯：每只涨停股 + 它所属的**精选板块**代码与名称。

        这是「个股 → 板块」的唯一可靠来源。实时与历史都通（一个接口两种域名），
        实测 09-18 得 77 家、09-21 得 101 家，与站内涨停池的 78 / 103 基本吻合。

        返回按连板数降序（接口本身就是这个顺序）。
        """
        payload = self._post(
            trade_date,
            {"a": "GetZhangTingTianTi", "c": "FuPanLa"},
            f"开盘红涨停天梯 {trade_date}",
        )
        stocks = payload.get("StockList") or []
        if not stocks:
            # 空列表既可能是「当天真的没有涨停」也可能是接口变了。涨停家数有
            # 其他来源可以交叉验证，这里只如实记一行，让上层去对账。
            logger.warning("开盘红涨停天梯 %s 返回 0 行", trade_date)

        rows = []
        for row in stocks:
            def cell(index: int, _row: list = row) -> object:
                return _row[index] if index < len(_row) else None

            code = str(cell(LADDER_CODE) or "").strip()
            board_code = str(cell(LADDER_BOARD_CODE) or "").strip()
            if not code or not board_code:
                continue
            rows.append(
                {
                    "code": code,
                    "name": str(cell(LADDER_NAME) or "").strip(),
                    "consecutive": _to_float(cell(LADDER_CONSECUTIVE)),
                    "seal_time": cell(LADDER_SEAL_TIME),
                    "board_code": board_code,
                    "board_name": str(cell(LADDER_BOARD_NAME) or "").strip(),
                    "board_limit_count": _to_float(cell(LADDER_BOARD_LIMIT_COUNT)),
                    "amount": _to_float(cell(LADDER_AMOUNT)),
                }
            )
        return rows
