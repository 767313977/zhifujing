"""应用配置：优先读环境变量，其次读项目根目录 .env。"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "backend" / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 服务 ---
    # 监听地址。127.0.0.1 = 只有本机能打开；0.0.0.0 = 同一 WiFi 下的手机 /
    # 平板也能打开。⚠️ 本服务**没有任何鉴权**，改成 0.0.0.0 之前想清楚这一点 ——
    # 家里网络可以接受，连公共 WiFi 时不要改。
    host: str = "127.0.0.1"
    port: int = 8000

    # --- iFinD ---
    ifind_auth_token: str = ""
    ifind_base_url: str = "https://api-mcp.51ifind.com:8643/ds-mcp-servers"
    # 个人版权益上限 5 请求/秒，留 1 余量避免触发限流
    ifind_rate_limit: float = 4.0
    # 账号级调用次数上限（tools/call 计费，所有链路共享）。
    # iFinD 官方口径是「每月 5000 次」，但**计量窗口按订阅周期滚动**，见下面的
    # ifind_cycle_start_day。形态选股的配额守卫按它算阈值，见设计文档 8.16.4。
    ifind_monthly_quota: int = 5000

    # 计量周期的起点日（订阅日）。iFinD 的计量窗口**不是自然月** —— 后台
    # 「计量区间」实测为 2026-09-17 16:55:33 ~ 2026-10-17 16:55:33，
    # 起止都落在订阅日。这个值填错会让配额守卫**低估**已用量：该让路时不动作，
    # 然后直接撞墙 —— 正好是它要防的那件事。
    # 上限卡在 28：29/30/31 在 2 月不存在，周期边界会漂，某几天的用量被重复
    # 计入或漏计；真赶上月末订阅就填 28，误差一两天，守卫宁可早让路。
    ifind_cycle_start_day: int = Field(default=17, ge=1, le=28)

    # --- akshare（仅用 push2ex 集群）---
    akshare_rate_limit: float = 1.0
    # 同花顺 q.10jqka.com.cn 与东财 push2 是两个站点，频控互不影响。
    # 概念板块要逐个拉指数（375 个），用 1/s 太慢，实测 3/s 未被限。
    ths_rate_limit: float = 3.0
    # 开盘红（开盘啦团队的新版 App）的 apphwshhq / apphis 又是两个独立站点。
    # 板块排行 270 个要翻 7 页、成分股单次可能几百 KB，实测 3/s 未被限。
    kph_rate_limit: float = 3.0

    # --- 通用 HTTP ---
    http_retries: int = 3
    http_backoff: float = 2.0
    # iFinD 自然语言工具较慢，实测最慢 4.1s，留足余量
    http_timeout: float = 60.0

    # --- 存储 ---
    db_path: Path = DATA_DIR / "fupan.db"
    log_level: str = "INFO"

    # iFinD 单次 symbols 上限，超出会被服务端静默丢弃
    ifind_max_symbols: int = 10

    # --- 定时采集 ---
    # 交易日收盘后的采集时刻。**不能贴着收盘取**：有几项数据在收盘那一刻还没发布，
    # 实测 15:25 取当天龙虎榜返回空、17:05 再取就有 42 行。而采集是「按当天取」的，
    # 取不到就留下一个没人回头补的洞 —— 龙虎榜机构席位（同一个东财 datacenter）同理。
    #
    # 2026-09-22 由 18:00 提到 17:30（想早点看到简报；简报是在采集跑完后紧接着发的，
    # 所以推送时刻也跟着提前）。⚠️ 这样离上面那次实测的「17:05 已有数据」只剩 25 分钟
    # 余量（原本 55 分钟）—— 哪天 17:30 采到的龙虎榜或机构席位为空，先怀疑这里，
    # 把它挪回 18:00 再看一眼。
    #
    # 两融/北向不受影响：那两项每次都回看 30 天，漏一天下次自然补上。
    scheduler_enabled: bool = True
    collect_hour: int = 17
    collect_minute: int = 30
    # 本机不常开，启动时若已过采集时刻且当日无数据，补采一次
    catchup_on_start: bool = True

    # --- 飞书推送 ---
    # 群自定义机器人的 webhook 地址。**首选通道**：它不依赖 Trae 的登录态，
    # 也不会过期，配上它之后 lark_cli 那条路就不会再走。
    feishu_webhook_url: str = ""
    # 备用通道的收件人：ou_ 开头=用户 open_id（自己给自己发），oc_ 开头=会话 id。
    # 只在没配 webhook 时生效。
    feishu_target: str = ""
    feishu_push_enabled: bool = True
    # Trae 插件自带的命令行工具，默认在 PATH 上
    lark_cli: str = "lark-cli"

    # --- 形态选股：股票池与日线 ---
    # 池子门槛：近 20 日日均成交额（元）。低于这个数的票出了形态也没法交易，
    # 采它们的日线纯属白花 iFinD 配额。
    # 注意实测：「2000 万」几乎不过滤（5569 只里只砍掉 291 只），量级得按亿来设。
    universe_min_amount: float = 1e8
    # 日线采集的批次大小**不再是配置项**：现在按「代码前缀 × 单个交易日」问，
    # 一天一个前缀一次调用（13 次覆盖全市场），没有可调的批大小。
    # 历史上这里有个 kline_batch_size=50，配合「50 只 × 一段区间」的批量行情接口 ——
    # 那个接口 2026-09-21 起不再提供 CSV，超过 100 行会静默返回抽样表，已弃用。
    # 首次建库拉多少个交易日的历史。
    # 2026-09-21 从 250 提到 **500（约 2 年）**：个股页的周/月 K 按 2 年画，
    # 而这个窗口也是「历史回补」的边界（`_window_start(full=True)` 用它）。
    # ⚠️ 一个完整窗口 ≈ 500 天 × 14 次 = 7000 次调用，**一轮月度配额装不下**，
    # 所以必须靠下面的 `kline_backfill_days` 每天往前啃一小段，见 scheduler。
    kline_history_days: int = 500
    # 每日增量回看的**日历日**跨度（留宽一点，覆盖最近几个交易日并容忍假期）。
    # 现在的作用是「顺带回补最近这些天里还缺数据的日子」。
    kline_refresh_calendar_days: int = 12
    # 每天自动往前啃几个交易日的历史（每个交易日 = 14 次调用）。
    # 4 天 ≈ 56 次/天 ≈ 1700 次/月，配上日常采集约 2100 次正好压在一轮配额里；
    # 按这个速度，从 250 天铺到 500 天要约 2 个月。
    kline_backfill_days: int = 4
    # 历史回补的**停手线**（用量占配额的比例）。
    # 特意比日线采集的让路线（80%，见 usage.QuotaLevel）更保守：这样
    # **回补永远不会把当天的日线采集顶停** —— 历史是慢慢补的，而当天的行情
    # 缺了就真缺了（数据源只保留最近 15 个交易日）。
    kline_backfill_max_ratio: float = 0.74
    # stock_daily 保留的交易日数。
    #
    # 形态最长看 250 日，而**周/月 K 是按需拉 2 年历史的**（打开个股页切到
    # 周/月时，前端会调一次 `sync?days=500`，约 500 个交易日 ≈ 24 个月）。
    # 这个值必须 ≥ 500，否则拉回来的历史会被 `prune()` 当晚剪掉最老的一段，
    # 白花那十几次 iFinD 调用。520 是 500 + 一点余量。
    kline_keep_days: int = 520


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
