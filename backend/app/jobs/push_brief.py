"""每日复盘简报：拼 Markdown，推送到飞书。

两件事放在一个模块里，因为调用方只有一个意图 ——「把今天的复盘发给我」，
拆开反而要在外面传一大堆中间态。

简报内容**全部来自本地库**（只读，不碰外部数据源），所以它随时可以重发，
推送失败也绝不会反过来影响采集结果。

两条发送通道，**webhook 优先**：

1. **群自定义机器人 webhook**（`FEISHU_WEBHOOK_URL`）—— 地址长期有效，
   纯 HTTP POST，不依赖任何登录态。正式通道。
2. **`lark-cli` 用户身份**（`FEISHU_TARGET`）—— 用来在还没建群机器人时先跑通。
   它的凭据来自环境变量 `LARKSUITE_CLI_USER_ACCESS_TOKEN`，由 Trae 在
   **拉起进程时**注入，有效期只有约 2 小时；后端是常驻进程，15:05 触发时用的
   仍是它启动那一刻的 Token，所以这条路会随后端运行时长而失效
   （表现为 `230027 access denied`）。只在没配 webhook 时才走。
"""

import json
import logging
import subprocess
from datetime import date

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import (
    CollectLog,
    IndexDaily,
    Lhb,
    LimitPool,
    MarketSentiment,
    PatternHit,
    SectorDaily,
)
from app.services.patterns import PATTERN_NAMES
from app.services.themes import limit_up_themes
from app.services.usage import QuotaLevel, level_label, quota_level, quota_status
from app.sources.kaipanhong import TAXONOMY_INDUSTRY, TAXONOMY_SELECTED

logger = logging.getLogger(__name__)

# 采集日志里的一类任务名，同时用来给「当天是否已推送」去重
PUSH_TASK = "push_brief"
# 配额对账提醒。它按「距上次提醒多少天」判断该不该发，任务名同时是那条判断的依据
CALIBRATION_TASK = "calibration_reminder"
CALIBRATION_INTERVAL_DAYS = 15

# 会**单独推一条消息**的形态：`形态 key → (标题, 采集日志任务名, 一句话描述, 买点提示)`。
#
# 为什么不并进每日简报的「形态选股」那一节：那节按命中家数降序、每形态只列前 3，
# 而这两个形态都稀疏（共达模式每天十几只，欧奈尔收紧后常常只有 1 只），永远排在
# 最后，扫一眼注意不到。独立推送才能做到「有票就响」。
#
# 每个形态用**自己的任务名**去重：它们与复盘简报的发送条件不同（一个有命中才发、
# 一个每天必发），混用同一个名字会互相顶掉。
PUSH_PATTERNS: dict[str, tuple[str, str, str, str]] = {
    "limit_surge_flat": (
        "⭐ 共达模式选股",
        "push_gongda",
        "涨停爆量 → 突破平台 → 缩量横盘，等二次启动",
        "买点：放量站上突破价 · 跌破支撑价作废",
    ),
    "oneil_breakout": (
        "⭐ 欧奈尔突破",
        "push_oneil",
        "基底整理 → 放量突破平台上沿 → 贴近一年新高",
        "买点：突破枢轴点当天买入 · 跌破基底下沿作废",
    ),
}

DASH = "—"
# 与前端 lib/format.ts 的金额口径保持一致
WAN, YI, WANYI = 1e4, 1e8, 1e12

# 2 板及以上每档都点名，首板只报家数（首板动辄几十只，全列出来会把简报撑爆）
NAMED_CONSECUTIVE = 2
# 板块热力领涨/领跌各列几个
HEAT_LEADERS = 5
# 龙虎榜净买额取前几
LHB_TOP = 5
# 简报里每个形态列几只。简报是扫一眼用的，不是挑票用的
PATTERN_TOP = 3
# 共振题材按涨停家数取前几。库里一天能聚出几十个板块，推送里全列出来没人看 ——
# 页面可以滚动，消息不行
THEME_TOP = 6

# 旧版这里有一张 CHANNEL_CONCEPTS 黑名单（融资融券 / 沪股通 / MSCI概念 …），
# 用来把「交易通道与指数纳入口径」这类伪概念从共振题材里滤掉。换开盘红之后
# **整张名单作废**：它的「精选板块」里根本没有这几个名字（270 个板块逐一核对过），
# 所以直接删掉，而不是留一张永远不会命中的表。若将来「次新股 / ST板块」这类
# 开始霸榜，再按当时的实际名字补。

_WEEKDAY = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# lark-cli 发送权限不足时的错误码。专门提出来是因为它最常见、也最好解释
_SCOPE_DENIED = 230027


# ---------------------------------------------------------------------- 格式化


def _pct(value: float | None, digits: int = 2) -> str:
    """涨跌幅始终带符号 —— 行首符号是判断方向最快的线索。"""
    return DASH if value is None else f"{value:+.{digits}f}%"


def _rate(value: float | None) -> str:
    """比率（封板率 / 炸板率）不带符号：它们没有负值，带 + 号只会让人误读成涨幅。"""
    return DASH if value is None else f"{value:.2f}%"


def _amount(value: float | None) -> str:
    if value is None:
        return DASH
    for unit, scale in (("万亿", WANYI), ("亿", YI), ("万", WAN)):
        if abs(value) >= scale:
            return f"{value / scale:.2f}{unit}"
    return f"{value:.0f}"


def _count(value: int | None) -> str:
    """家数为 None 表示「当日取不到数」，与 0 家严格区分，一律显示 —。"""
    return DASH if value is None else str(value)


def _price(value: float | None) -> str:
    return DASH if value is None else f"{value:,.2f}"


# ------------------------------------------------------------------------ 分节


def _index_block(session: Session, trade_date: date) -> str:
    # 延迟导入：collect_daily 会（间接）用到本模块，模块级导入会成环
    from app.jobs.collect_daily import INDEX_NAMES

    rows = {
        row.code: row
        for row in session.scalars(
            select(IndexDaily).where(IndexDaily.trade_date == trade_date)
        )
    }
    lines = [
        f"- {name} {_price(rows[code].close)} {_pct(rows[code].pct_chg)}"
        for code, name in INDEX_NAMES.items()
        if code in rows
    ]
    return _section("指数", lines)


def _sentiment_block(session: Session, trade_date: date) -> str:
    row = session.scalar(
        select(MarketSentiment).where(MarketSentiment.trade_date == trade_date)
    )
    if row is None:
        return ""
    return _section(
        "情绪",
        [
            f"- 涨停 {_count(row.limit_up_count)} · 跌停 {_count(row.limit_down_count)}"
            f" · 炸板 {_count(row.broken_count)}",
            f"- 封板率 {_rate(row.seal_rate)} · 炸板率 {_rate(row.broken_rate)}",
            f"- 最高连板 {_count(row.max_consecutive)} 板",
            f"- 上涨 {_count(row.up_count)} / 下跌 {_count(row.down_count)}",
            f"- 两市成交额 {_amount(row.total_amount)}",
            f"- 昨日涨停股今日均涨 {_pct(row.yesterday_limit_today_avg)}",
        ],
    )


def _ladder_block(session: Session, trade_date: date) -> str:
    rows = list(
        session.scalars(
            select(LimitPool)
            .where(LimitPool.trade_date == trade_date, LimitPool.pool_type == "up")
            .order_by(LimitPool.consecutive.desc().nullslast())
        )
    )
    if not rows:
        return ""

    buckets: dict[int, list[str]] = {}
    for row in rows:
        # 连板数为空按首板处理，与 /api/limit/pool 的梯队口径一致
        buckets.setdefault(row.consecutive or 1, []).append(row.name or row.code)

    lines = [
        f"- {level} 板（{len(names)}）：{'、'.join(names)}"
        for level, names in sorted(buckets.items(), reverse=True)
        if level >= NAMED_CONSECUTIVE
    ]
    first = len(buckets.get(1, []))
    if first:
        lines.append(f"- 首板：{first} 家")
    return _section("连板梯队", lines)


def _theme_block(session: Session, trade_date: date) -> str:
    clusters, _ = limit_up_themes(session, trade_date)
    lines = [
        f"- {item.concept} {item.count} 家 {_pct(item.pct_chg)}"
        for item in clusters[:THEME_TOP]
    ]
    if len(clusters) > THEME_TOP:
        lines.append(f"- 其余 {len(clusters) - THEME_TOP} 个题材见站点「涨停复盘」")
    return _section("题材共振（按涨停家数）", lines)


def _sector_block(session: Session, trade_date: date) -> str:
    def _top(taxonomy: str, reverse: bool) -> list[str]:
        rows = list(
            session.scalars(
                select(SectorDaily)
                .where(
                    SectorDaily.trade_date == trade_date,
                    SectorDaily.taxonomy == taxonomy,
                    SectorDaily.pct_chg.is_not(None),
                )
                .order_by(
                    SectorDaily.pct_chg.desc() if reverse else SectorDaily.pct_chg.asc()
                )
                .limit(HEAT_LEADERS)
            )
        )
        return [f"{row.name or row.sector_code} {_pct(row.pct_chg)}" for row in rows]

    lines = []
    for label, taxonomy in (("精选", TAXONOMY_SELECTED), ("行业", TAXONOMY_INDUSTRY)):
        leaders = _top(taxonomy, reverse=True)
        if leaders:
            lines.append(f"- {label}领涨：{' · '.join(leaders)}")
        laggards = _top(taxonomy, reverse=False)
        if laggards:
            lines.append(f"- {label}领跌：{' · '.join(laggards)}")
    return _section("板块热力", lines)


def _lhb_block(session: Session, trade_date: date) -> str:
    rows = list(
        session.scalars(
            select(Lhb)
            .where(Lhb.trade_date == trade_date)
            .order_by(Lhb.net_buy.desc().nullslast())
            .limit(LHB_TOP)
        )
    )
    lines = [
        f"- {row.name or row.code} {_amount(row.net_buy)} {_pct(row.pct_chg)}"
        for row in rows
        if row.net_buy is not None
    ]
    return _section(f"龙虎榜净买额前 {LHB_TOP}", lines)


def build_pattern_brief(trade_date: date, pattern: str) -> str:
    """某个形态当天的独立推送内容（形态清单见 `PUSH_PATTERNS`）。

    当日没有命中就返回空串，调用方据此不发。
    """
    title, _task, desc, hint = PUSH_PATTERNS[pattern]
    with session_scope() as session:
        rows = session.execute(
            select(
                PatternHit.code,
                PatternHit.name,
                PatternHit.score,
                PatternHit.close,
                PatternHit.pct_chg,
                PatternHit.key_levels,
            )
            .where(
                PatternHit.trade_date == trade_date,
                PatternHit.pattern == pattern,
            )
            .order_by(PatternHit.score.desc())
        ).all()
    if not rows:
        return ""

    lines = [
        # 标题带「收盘复盘」：飞书群机器人多半配了**自定义关键词**校验，消息里
        # 必须含那个词才放行（实测漏了它直接报 `19024 Key Words Not Found`）。
        # 与简报标题同一格式，既满足限制又不显得突兀
        f"**收盘复盘 · {title} · {trade_date.isoformat()}**",
        f"共 **{len(rows)}** 只：{desc}",
        "",
    ]
    for code, name, score, close, pct, levels in rows:
        levels = levels or {}
        lines.append(
            f"- **{name or code}** {code}（{score:.0f} 分）："
            f"突破 **{_price(levels.get('breakout'))}** / "
            f"支撑 {_price(levels.get('support'))} · 现价 {_price(close)}（{_pct(pct)}）"
        )
    lines += ["", hint]
    return "\n".join(lines)


def _pattern_block(session, trade_date: date) -> str:
    """形态选股：各形态命中家数 + 每个形态最强的几只。

    只列前三名 —— 简报是给人扫一眼「今天发生了什么」的，不是给人挑票用的；
    挑票去页面上按形态和评分筛。
    """
    rows = session.execute(
        select(PatternHit.pattern, PatternHit.name, PatternHit.code, PatternHit.score)
        .where(PatternHit.trade_date == trade_date)
        .order_by(PatternHit.pattern, PatternHit.score.desc())
    ).all()
    if not rows:
        return ""

    grouped: dict[str, list[tuple[str | None, str, float]]] = {}
    for pattern, name, code, score in rows:
        grouped.setdefault(pattern, []).append((name, code, score))

    lines = [f"- 共 **{len(rows)}** 条命中"]
    # 按命中家数降序：一眼就能看出「今天什么形态最普遍」
    for pattern, items in sorted(grouped.items(), key=lambda pair: -len(pair[1])):
        label = PATTERN_NAMES.get(pattern, pattern)
        top = "、".join(
            f"{name or code}（{score:.0f}）" for name, code, score in items[:PATTERN_TOP]
        )
        lines.append(f"- **{label}** {len(items)} 只：{top}")
    return _section("形态选股", lines)


def _quota_block() -> str:
    """配额吃紧时在简报里明说。

    被配额让路跳过的采集步骤平时只出现在日志里，而日报是唯一每天必看的东西 ——
    某些数据缺了却不说，就会被读成「那天真的没问题」。所以这里必须显式提一句。
    """
    level = quota_level()
    if level == QuotaLevel.NORMAL:
        return ""
    return f"**iFinD 配额告警**\n- {level_label(level)}"


def _section(title: str, lines: list[str]) -> str:
    """一节为空就整节不出现 —— 空标题比缺一节更像故障。"""
    if not lines:
        return ""
    return f"**{title}**\n" + "\n".join(lines)


# ------------------------------------------------------------------------ 组装


def latest_trade_date() -> date | None:
    """库里最新的交易日。

    以情绪表为准：它是采集流程的最后一步，有它说明前面几步都跑过了。
    """
    with session_scope() as session:
        return session.scalar(select(func.max(MarketSentiment.trade_date)))


def build_brief(trade_date: date) -> str:
    """把某个交易日的复盘要点拼成一段飞书可渲染的 Markdown。

    故意**不用表格**：飞书消息体是富文本（post），不支持 Markdown 表格，
    写进去只会渲染成一串竖线原文。全部用「小节标题 + 列表」表达。
    """
    with session_scope() as session:
        blocks = [
            f"**收盘复盘 · {trade_date.isoformat()}（{_WEEKDAY[trade_date.weekday()]}）**",
            # 配额告警放最前：它是「这份简报本身可能不完整」的前提说明
            _quota_block(),
            _index_block(session, trade_date),
            _sentiment_block(session, trade_date),
            _ladder_block(session, trade_date),
            _theme_block(session, trade_date),
            _sector_block(session, trade_date),
            _lhb_block(session, trade_date),
            # 「共达模式选股」不在这里 —— 它有命中时单独发一条
            # （`build_gongda_brief` + `push_gongda`），混进复盘简报会被淹掉
            _pattern_block(session, trade_date),
        ]
    return "\n\n".join(block for block in blocks if block)


# ------------------------------------------------------------------------ 发送


def _post_card(webhook: str, elements: list[dict]) -> dict:
    """把**卡片元素列表**发给群自定义机器人。

    为什么收的是元素而不是一整段 markdown：飞书的**表格组件只能挂在卡片根节点**、
    不能塞进 `div` 里 —— 带表格的推送只能自己拼 `elements`。
    """
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "elements": elements,
        },
    }
    try:
        response = requests.post(webhook, json=payload, timeout=30)
    except requests.RequestException as exc:
        return {"ok": False, "error": f"请求 webhook 失败: {exc}"}

    try:
        body = response.json()
    except ValueError:
        return {
            "ok": False,
            "error": f"webhook 返回非 JSON（HTTP {response.status_code}）",
        }

    # 注意这里判的是 `code == 0`，与下面 lark-cli 的 `ok == true` 正相反 ——
    # 群自定义机器人走的是**老格式**信封，照抄另一边会把成功判成失败。
    if body.get("code") == 0:
        return {"ok": True}
    return {"ok": False, "error": f"{body.get('code')} {body.get('msg')}"}


def _send_via_lark_cli(markdown: str, settings: Settings) -> dict:
    """用本机 `lark-cli` 把 Markdown 发给配置的收件人。

    判定成功一律看 `ok == true`（lark-shared 的合约）——
    成功信封里没有顶层 `code`，按 `code == 0` 判断会把成功当失败。
    """
    target = settings.feishu_target.strip()
    # ou_ 是用户 open_id（自己给自己发），oc_ 是会话 id
    flag = "--user-id" if target.startswith("ou_") else "--chat-id"
    argv = [
        settings.lark_cli,
        "im",
        "+messages-send",
        flag,
        target,
        "--markdown",
        markdown,
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        return {"ok": False, "error": f"没找到 {settings.lark_cli}，它只在 Trae 环境里可用"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "lark-cli 超过 60 秒没有响应"}

    try:
        payload = json.loads(proc.stdout or "")
    except json.JSONDecodeError:
        return {"ok": False, "error": f"lark-cli 输出无法解析: {proc.stdout or proc.stderr}"}

    if payload.get("ok"):
        return {"ok": True, "message_id": payload.get("data", {}).get("message_id")}

    error = payload.get("error") or {}
    hint = ""
    if error.get("code") == _SCOPE_DENIED:
        # 最常见的失败就是它，直接把原因写清楚，不用再去翻日志
        hint = "（Token 过期或权限不足，重启后端即可拿到新 Token）"
    return {
        "ok": False,
        "error": f"{error.get('code')} {error.get('message')}{hint}".strip(),
    }


def send_elements(
    elements: list[dict], settings: Settings, *, fallback_markdown: str = ""
) -> dict:
    """发一组**卡片元素**（可含表格）。

    **webhook 优先**：群自定义机器人的地址长期有效，而 lark-cli 那条路靠 Trae 注入的
    2 小时 Token，随时可能失效。没配 webhook 时退回 lark-cli 发 `fallback_markdown`
    —— 那条通道只吃 markdown，**表格发不出去**，所以调用方要给一份文字版兜底。
    """
    webhook = settings.feishu_webhook_url.strip()
    if webhook:
        return _post_card(webhook, elements)
    if not fallback_markdown:
        return {"ok": False, "error": "没有配 webhook，且没有提供可退回的文字版"}
    return _send_via_lark_cli(fallback_markdown, settings)


def send_markdown(markdown: str, settings: Settings) -> dict:
    """把一段 markdown 当**一个**卡片块发出去（简报、形态推送都走它）。"""
    element = {"tag": "div", "text": {"tag": "lark_md", "content": markdown}}
    return send_elements([element], settings, fallback_markdown=markdown)


# ------------------------------------------------------------------ 去重与记录


def already_pushed(trade_date: date, task: str = PUSH_TASK) -> bool:
    """当天这一类消息是否已成功推送过。

    去重是必需的：服务重启、启动补采都会走到推送这一步，
    一天推好几遍比漏推更烦人。`task` 用来区分「复盘简报」与「共达模式选股」——
    两条消息各有各的去重，不会互相顶掉。
    """
    with session_scope() as session:
        return bool(
            session.scalar(
                select(func.count())
                .select_from(CollectLog)
                .where(
                    CollectLog.trade_date == trade_date,
                    CollectLog.task == task,
                    CollectLog.status == "ok",
                )
            )
        )


def _record(
    trade_date: date, status: str, message: str, task: str = PUSH_TASK
) -> None:
    """记一条采集日志。

    刻意不填 `rows`：那一列在「数据管理」页读作**行数**，
    推送没有行数可言，写个字数进去只会让它显示成「932 行」。
    """
    with session_scope() as session:
        session.add(
            CollectLog(
                trade_date=trade_date,
                task=task,
                status=status,
                message=message,
            )
        )


def push(trade_date: date | None = None, settings: Settings | None = None) -> dict:
    """生成并推送简报。返回的 `status` 取值：ok / skipped / failed。

    **不抛异常**：推送是采集流程的收尾步骤，失败只记日志，
    绝不能把已经成功的采集标成失败。
    """
    settings = settings or get_settings()
    if not settings.feishu_push_enabled:
        return {"status": "skipped", "reason": "推送已关闭（FEISHU_PUSH_ENABLED=false）"}
    if not settings.feishu_webhook_url.strip() and not settings.feishu_target.strip():
        return {
            "status": "skipped",
            "reason": "未配置 FEISHU_WEBHOOK_URL（群机器人）或 FEISHU_TARGET（收件人）",
        }

    target_date = trade_date or latest_trade_date()
    if target_date is None:
        return {"status": "skipped", "reason": "库里还没有任何交易日数据"}

    markdown = build_brief(target_date)
    result = send_markdown(markdown, settings)
    status = "ok" if result["ok"] else "failed"
    _record(
        target_date,
        status,
        f"已推送 {len(markdown)} 字" if result["ok"] else result["error"],
    )
    logger.info(
        "简报 %s %s（%d 字）",
        target_date,
        "已推送" if result["ok"] else "推送失败",
        len(markdown),
    )
    return {
        "status": status,
        "trade_date": target_date.isoformat(),
        "chars": len(markdown),
        **result,
    }


def push_pattern_brief(
    pattern: str, trade_date: date | None = None, settings: Settings | None = None
) -> dict:
    """推送某个形态的独立消息。`status` 取值同 `push()`。

    **当日没有命中就不发**（返回 `skipped`）—— 所以它是「有票才响」，
    不像复盘简报那样每天都在。去重交给调用方
    （`already_pushed(date, PUSH_PATTERNS[pattern][1])`），与 `push()` 的分工一致。
    """
    title, task, _desc, _hint = PUSH_PATTERNS[pattern]
    settings = settings or get_settings()
    if not settings.feishu_push_enabled:
        return {"status": "skipped", "reason": "推送已关闭（FEISHU_PUSH_ENABLED=false）"}
    if not settings.feishu_webhook_url.strip() and not settings.feishu_target.strip():
        return {
            "status": "skipped",
            "reason": "未配置 FEISHU_WEBHOOK_URL（群机器人）或 FEISHU_TARGET（收件人）",
        }

    target_date = trade_date or latest_trade_date()
    if target_date is None:
        return {"status": "skipped", "reason": "库里还没有任何交易日数据"}

    markdown = build_pattern_brief(target_date, pattern)
    if not markdown:
        # 没命中**不写日志**：这些形态本来就稀疏，安静跳过是常态；
        # 写一条 skipped 只会让「数据管理」页天天多一行无意义的记录
        return {"status": "skipped", "reason": f"当日无 {pattern} 命中"}

    result = send_markdown(markdown, settings)
    status = "ok" if result["ok"] else "failed"
    _record(
        target_date,
        status,
        f"{title} 已推送 {len(markdown)} 字" if result["ok"] else result["error"],
        task,
    )
    logger.info(
        "%s %s %s（%d 字）",
        title,
        target_date,
        "已推送" if result["ok"] else "推送失败",
        len(markdown),
    )
    return {
        "status": status,
        "trade_date": target_date.isoformat(),
        "chars": len(markdown),
        **result,
    }


def build_calibration_reminder(remind_date: date) -> str:
    """配额对账提醒的内容。

    提醒的是**人去核对**，不是自动校准 —— 后台的真实数字只有人能读到
    （iFinD 没有查用量的接口），所以这条消息的作用是把他推到后台看一眼。
    """
    data = quota_status()
    return "\n".join(
        [
            # 标题带「收盘复盘」：群机器人配了自定义关键词校验，
            # 消息里不含那个词会被直接拒掉（共达模式那节记过这个坑）
            f"**收盘复盘 · 配额对账提醒 · {remind_date.isoformat()}**",
            f"每 {CALIBRATION_INTERVAL_DAYS} 天对一次账，花一分钟核一下。",
            "",
            f"- 本地记录：**{data['cycle_calls']} / {data['monthly_quota']}**"
            f"（本周期 {data['cycle_start']} ~ {data['cycle_end']}，"
            f"剩余 {data['cycle_remaining']}）",
            "- iFinD 后台：打开「个人版套餐」那张卡片，看**计量区间**那一行的已用次数",
            "",
            "两边不一致（本地偏小）时，在电脑上跑：",
            "`python scripts/calibrate_quota.py <后台的数字>`",
            "",
            "本地偏小是常态（手动查询、别的设备都计入同一个账号配额），"
            "但**低估会让让路机制晚触发**，所以隔一阵要对一次。",
        ]
    )


def last_calibration_reminder() -> date | None:
    """上一次对账提醒推在哪天；从没推过返回 None。"""
    with session_scope() as session:
        return session.scalar(
            select(func.max(CollectLog.trade_date)).where(
                CollectLog.task == CALIBRATION_TASK,
                CollectLog.status == "ok",
            )
        )


def push_calibration_reminder(
    remind_date: date | None = None, settings: Settings | None = None
) -> dict:
    """推送配额对账提醒。

    这里**不管间隔**（「距上次满 15 天了吗」由 scheduler 判断）——
    与 `push()` / `push_gongda()` 的分工保持一致：这两个函数只管生成与发送。
    """
    settings = settings or get_settings()
    if not settings.feishu_push_enabled:
        return {"status": "skipped", "reason": "推送已关闭（FEISHU_PUSH_ENABLED=false）"}
    if not settings.feishu_webhook_url.strip() and not settings.feishu_target.strip():
        return {
            "status": "skipped",
            "reason": "未配置 FEISHU_WEBHOOK_URL（群机器人）或 FEISHU_TARGET（收件人）",
        }

    target_date = remind_date or date.today()
    markdown = build_calibration_reminder(target_date)
    result = send_markdown(markdown, settings)
    status = "ok" if result["ok"] else "failed"
    # 推失败也记一条，但用 failed —— `last_calibration_reminder()` 只认 ok，
    # 所以失败了下次还会再提醒，不会因为一次网络抖动就哑掉半个月
    _record(
        target_date,
        status,
        f"对账提醒已推送 {len(markdown)} 字" if result["ok"] else result["error"],
        CALIBRATION_TASK,
    )
    logger.info(
        "对账提醒 %s %s（%d 字）",
        target_date,
        "已推送" if result["ok"] else "推送失败",
        len(markdown),
    )
    return {
        "status": status,
        "trade_date": target_date.isoformat(),
        "chars": len(markdown),
        **result,
    }
