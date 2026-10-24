"""FastAPI 入口。"""

import logging
import socket
import threading
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    admin,
    analysis,
    anomaly_map,
    auction,
    auth as auth_api,
    funds,
    journal,
    limit,
    market,
    note,
    patterns,
    plans,
    sector,
    similar,
    stock,
    valuation,
    watchlist,
)
from app.config import ROOT, get_settings
from app.db import init_db
from app.jobs.scheduler import start_scheduler, stop_scheduler
from app.services.auth import require_login

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# 再往文件写一份。这个服务平时是双击 start.bat 起来的、或由计划任务在后台跑，
# 那些场景下控制台没有去处 —— 真出错时什么都留不下，排查只能靠猜。
# 用轮转而不是单个文件：一次采集就能刷几百行，不轮转迟早撑成几十 MB。
_log_file = settings.db_path.parent / "fupan.log"
_file_handler = RotatingFileHandler(
    _log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
)
logging.getLogger().addHandler(_file_handler)

logger = logging.getLogger(__name__)

# 前端构建产物；开发期前端跑在 Vite 的 5173 并由其代理到本服务
FRONTEND_DIST = ROOT / "frontend" / "dist"


def _warm_pattern_cache() -> None:
    """后台预热形态成绩单缓存（2026-10-23 加，task-7 #12）。

    `/api/patterns/scorecard` 是站内最重的读接口（线上冷缓存实测 4.76s，`standing`
    1.5s、`track` 0.7s；热了之后 40ms），结果缓存在 `api/patterns._TRACK_CACHE`
    （10 分钟 TTL + 数据版本键）。部署 / 重启后第一个打开形态页的人要白等这几秒，
    所以启动后**用前端默认请求的参数**各跑一遍，把缓存捂热。

    ⚠️ 三条硬约定：
    - **守护线程 + 非阻塞**：调用方（lifespan）只 `start()` 不 join，启动不受影响；
    - **每一步独立 try/except**：任何一步失败都只记日志，绝不影响服务；
    - 参数必须与**前端默认值**一致，否则缓存键对不上、等于白跑（对应关系见
      `frontend/src/api/client.ts` 的 `patternScorecard/poolStanding/patternTrack`）。
    """
    from app.api import patterns as patterns_api
    from app.db import SessionLocal
    from app.services import pattern_track

    steps = (
        (
            "scorecard",
            lambda db: patterns_api.scorecard(
                db=db,
                cohorts=pattern_track.DEFAULT_COHORTS,
                track_days=max(pattern_track.HORIZONS),
                line_days=5,
                dedup_days=pattern_track.SCORECARD_DEDUP_DAYS,
            ),
        ),
        (
            "standing",
            lambda db: patterns_api.standing(
                db=db,
                cohorts=pattern_track.DEFAULT_COHORTS,
                track_days=max(pattern_track.HORIZONS),
                line_days=5,
            ),
        ),
        (
            "track",
            lambda db: patterns_api.track(
                db=db,
                cohorts=pattern_track.DEFAULT_COHORTS,
                top=pattern_track.DEFAULT_TOP,
                track_days=pattern_track.DEFAULT_TRACK_DAYS,
            ),
        ),
    )
    logger.info("形态成绩单缓存预热开始（后台线程，%d 步）", len(steps))
    total_started = time.monotonic()
    ok = 0
    for name, compute in steps:
        started = time.monotonic()
        try:
            with SessionLocal() as db:
                compute(db)
        except Exception:  # noqa: BLE001 - 预热失败绝不能影响服务启动
            logger.exception(
                "形态缓存预热 %s 失败（耗时 %.1f ms）",
                name,
                (time.monotonic() - started) * 1000,
            )
        else:
            ok += 1
            logger.info(
                "形态缓存预热 %s 完成：耗时 %.1f ms",
                name,
                (time.monotonic() - started) * 1000,
            )
    logger.info(
        "形态成绩单缓存预热结束：%d/%d 步成功，总耗时 %.1f ms",
        ok,
        len(steps),
        (time.monotonic() - total_started) * 1000,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    logger.info("数据库就绪: %s", settings.db_path)
    if not settings.ifind_auth_token:
        logger.warning("未配置 IFIND_AUTH_TOKEN，iFinD 相关采集将失败")
    if not FRONTEND_DIST.exists():
        logger.warning("未找到前端构建产物 %s，开发期请另起 Vite", FRONTEND_DIST)

    # 定时采集随服务一起启停。注意本服务必须单 worker 运行，
    # 多 worker 会各自拉起一个调度器、同一时刻重复采集。
    start_scheduler()

    # 形态成绩单缓存预热：后台守护线程，**不阻塞启动、失败只记日志**（见
    # `_warm_pattern_cache`）。放在 start_scheduler() 之后：调度器起来再抢 CPU 预热，
    # 不拖慢端口监听。
    if settings.warmup_pattern_cache:
        threading.Thread(
            target=_warm_pattern_cache,
            name="warmup-pattern-cache",
            daemon=True,
        ).start()

    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(
    title="致富经",
    version="0.3.0",
    lifespan=lifespan,
    # **关掉在线文档**（`/docs`、`/redoc`、`/openapi.json`）。这三条路由在**根路径**上，
    # 而下面的 require_login 只拦 `/api/` 前缀 —— 于是它们一直是免登录的：实测
    # `curl https://.../openapi.json` 返回 200，把整套 API（含 `/api/admin/*` 的参数
    # 与 schema）白送出去（2026-10-10 修）。这是个人站，**不需要在线文档**，且此前
    # 没有任何前端 / 部署脚本依赖 `/openapi.json`（已搜 frontend/ 与 deploy/）。
    # 直接关掉比把三条路由逐个塞进白名单更省事，也不会漏掉将来 FastAPI 新增的文档路由。
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    # **全站登录关卡**。挂成 app 级依赖而不是逐个路由挂：20 多个 `/api/*` 路由，
    # 一个一个挂漏掉一个就是一个洞，将来新加路由也容易忘。这里是白名单制、
    # **默认拒绝**（名单在 services/auth.PUBLIC_PATHS）。
    #
    # 为什么不是 `@app.middleware("http")`：中间件跑在事件循环里，而这里要查一次库
    # （拿会话 → 拿用户）。同步依赖会被 FastAPI 丢到线程池执行，不会堵住事件循环。
    #
    # ⚠️ 挂在 mount 上的 `/assets`（StaticFiles）不受 app 级依赖约束 —— 静态资源
    # 本来就该公开，登录页要用。SPA 回退那条路由会被这个依赖看到，但
    # require_login 对非 `/api/` 路径直接放行。
    dependencies=[Depends(require_login)],
)

_DEV_CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
app.add_middleware(
    CORSMiddleware,
    # 只放行前端**实际用到**的方法与请求头（2026-10-12 收紧）。站点本体与后端同源，
    # 这层 CORS 只服务本地 5173 的开发调试；原来写 `*` 等于任何来源都能跨域打这些接口
    #（虽未开 `allow_credentials`、影响有限，但没有必要留着这个敞口）。
    #
    # ⚠️ 开发源由 `settings.enable_dev_cors` 控制（2026-10-23 加）：生产 systemd 里设
    # `ENABLE_DEV_CORS=false` 就得到空名单，不再把 localhost:5173 送给线上的浏览器。
    allow_origins=_DEV_CORS_ORIGINS if settings.enable_dev_cors else [],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    """给前端产物加缓存头 —— 不加的话**部署完了用户还在看旧版**。

    构建产物的文件名带内容哈希（`index-B55kqZYy.js`），内容一变名字就变，所以
    `/assets/*` 可以放心长缓存（`immutable`）。

    而 `index.html` 必须每次重新校验。它是**引用哈希名的那一份**：一旦被浏览器
    启发式缓存住（`StaticFiles` / `FileResponse` 默认不发 `Cache-Control`，浏览器
    就会按「距 Last-Modified 过了多久」自己猜一个有效期），它指向的旧哈希产物在
    服务器上已经被删了，页面只能从缓存里拼出旧版 —— **实测 2026-09-26 踩过**：
    部署后云端三个产物与本机逐字节一致、`index.html` 也确实指向新哈希，
    但用户刷新仍是旧界面。`no-cache` 是「可以存、但每次必须校验」，正好够用。

    ⚠️ **长缓存只给成功响应**（2026-10-12 修）：原先不看状态码，于是资源被误删时
    返回的 **404 也被打上 `immutable, max-age=1 年`** —— 浏览器把「这个文件不存在」
    缓存一整年。文件恢复后页面**仍然白屏**，只有硬刷新/清缓存才恢复（当天线上事故
    的第二层原因）。非 2xx/304 一律 `no-store`。
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/"):
        if response.status_code in (200, 304):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            # 404/500 之类**绝不能**长缓存：内容随时会变（文件补回来、故障恢复）
            response.headers["Cache-Control"] = "no-store"
    elif not path.startswith("/api/"):
        # 接口不碰（默认就不缓存）：只覆盖 index.html 与 SPA 回退出来的那些静态文件
        response.headers["Cache-Control"] = "no-cache"
    return response


app.include_router(auth_api.router)
app.include_router(market.router)
app.include_router(auction.router)
app.include_router(limit.router)
app.include_router(funds.router)
app.include_router(sector.router)
app.include_router(stock.router)
app.include_router(analysis.router)
app.include_router(watchlist.router)
app.include_router(note.router)
app.include_router(journal.router)
app.include_router(plans.router)
app.include_router(similar.router)
app.include_router(patterns.router)
app.include_router(anomaly_map.router)
app.include_router(valuation.router)
app.include_router(admin.router)


@app.api_route("/api/health", methods=["GET", "HEAD"])
def health() -> dict:
    """探活接口。**只回最小状态**。

    ⚠️ **同时支持 HEAD**（2026-10-23 加）：监控/部署脚本常用 `curl -I`（HEAD），
    以前只挂 GET、HEAD 一律 405，会把「服务正常」判成失败。GET 行为不变。

    这里原来带回 `db`（数据库**绝对路径**）、`ifind_token`、`frontend_built`，那是给
    部署排查用的；但它挂在**免登录**的探活口上，等于对外泄露服务器路径与配置
    （2026-10-10 修）。前端与 `deploy/*.sh` / `_deploy_check.py` 都只按 200 判活、
    不读这些字段（已搜），需要细节看启动日志 —— db 路径与「未配置 token / 未构建前端」
    的告警都已经记在那里。
    """
    return {"status": "ok"}


#: SPA 兜底要**直接 404**（而不是回落 `index.html`）的路径特征。
#:
#: 为什么要有这个：兜底会把任何非 `/api` 路径都回一个 `index.html` + **200**，
#: 于是线上被漏洞扫描器扫到时看到的是一片 200（实测日志：`/.git/config`、
#: `/settings/.env`、`/webui/`、`/assets../.git/config` 全是 200）。虽然**没有实际
#: 泄漏** —— 回的是 index.html，那几段路径在 `dist/` 里根本不存在 —— 但「200」会让
#: 扫描器认为这里有东西、继续深挖，日志也被刷得没法看。
#:
#: 判据刻意保守，只挡「前端路由**不可能**出现」的形状：
#: 1. 任一路径段**以点开头**（`.git` / `.env` / `.svn` / `.DS_Store`…）；
#: 2. 一组扫描器常见的目录/文件名（下面这个集合）；
#: 3. 我们本来就不提供的扩展名（`.php` / `.sql` / `.zip`…）。
#:
#: ⚠️ **别把 `.svg` / `.png` / `.js` / `.css` 放进后缀表** —— `dist/` 里真有
#: `favicon.svg` / `wechat-qr.png` / `assets/*`，挡了它们页面直接白板。
#: ⚠️ 第 1 条会连 `.well-known/` 一起挡掉。本站的 ACME 证书是 nginx（webroot）在
#: 边缘签的，请求不会走到这里，所以没有影响；将来若改成由本服务出证书再回看这条。
_SCAN_SEGMENTS = frozenset(
    {
        "wp-admin", "wp-login", "wp-content", "wp-includes", "wp-config", "wordpress",
        "phpmyadmin", "pma", "myadmin", "webui", "cgi-bin", "actuator", "druid",
        "solr", "jenkins", "console", "server-status", "vendor", "backup", "backups",
        "config", "administrator", "telescope", "aws", "ssh", "env",
        # 2026-10-23 体检补：在线文档虽然已在 FastAPI 层关掉（docs_url=None 等），
        # 但兜底仍会把这些路径回 200 index.html —— 扫描器看到 200 会继续挖、日志也被刷。
        # 这里挡的是**噪声**，不是泄漏（回的是 SPA 外壳，没有 schema）。
        "docs", "redoc", "openapi.json",
    }
)
_SCAN_SUFFIXES = (
    ".php", ".asp", ".aspx", ".jsp", ".cgi", ".sql", ".bak", ".old", ".orig",
    ".zip", ".tar.gz", ".tgz", ".log", ".ini", ".yml", ".yaml", ".env",
    ".sh", ".py", ".rb",
    # 数据库文件（2026-10-23 体检补）：`/fupan.db`、`/fupan.db-wal` 之类也是扫描器常客。
    # 它们本来就不在 dist 里、回的是 index.html，这里只是别让它看到 200。
    ".db", ".db-wal", ".db-shm", ".sqlite",
)


def _is_scan_probe(full_path: str) -> bool:
    """这个非 `/api` 路径是不是扫描器探测？是就让兜底直接 404。"""
    segments = [seg for seg in full_path.split("/") if seg]
    if any(seg.startswith(".") for seg in segments):
        return True
    if any(seg.lower() in _SCAN_SEGMENTS for seg in segments):
        return True
    return full_path.lower().endswith(_SCAN_SUFFIXES)


def _spa_response(full_path: str) -> FileResponse:
    """SPA 兜底的核心（与路由注册分开，没构建前端时也能单测；见 tests/）。

    非 `/api` 路径一律交给前端路由；扫描器探测直接 404；带 null byte 等非法字符的
    路径**必须 404 而不是 500**（`Path.resolve()` 会抛 `ValueError: embedded null
    character in path`，2026-10-23 体检修）。
    """
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="接口不存在")
    # 控制字符（含 null byte）显式拦掉：Linux 上 `resolve()` 会抛 ValueError，
    # 但 **Windows 的 WindowsPath.resolve 不抛**（实测 2026-10-23，会把路径原样
    # 返回），于是同一条请求在 Windows 上会一路落到 index.html 回 200 ——
    # 两套系统行为不一致，所以这里先按「非法路径」自己判一次
    if any(ord(char) < 32 for char in full_path):
        raise HTTPException(status_code=404, detail="Not Found")
    # 扫描器探测（判据见 `_is_scan_probe`）：直接 404，别回 index.html 让它看到 200
    if _is_scan_probe(full_path):
        raise HTTPException(status_code=404, detail="Not Found")
    # resolve 掉 ../ 后必须仍在 dist 目录内，否则 /..%2F..%2F.env 这类请求
    # 能穿越出 dist 读到项目根的 .env（含 iFinD token）甚至数据库文件。
    # 同文件的 /assets 用 StaticFiles 自带穿越防护，这里手写的必须自己校验。
    try:
        candidate = (FRONTEND_DIST / full_path).resolve()
        inside = bool(full_path) and candidate.is_relative_to(FRONTEND_DIST)
        if inside and candidate.is_file():
            return FileResponse(candidate)
    except (ValueError, OSError):
        # null byte / 非法路径：这是**客户端输入问题**，不是服务端故障 —— 404 即可
        raise HTTPException(status_code=404, detail="Not Found") from None
    index = FRONTEND_DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    raise HTTPException(status_code=404, detail="Not Found")


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        """单页应用回退：非 /api 路径一律交给前端路由（GET / HEAD）。

        HEAD 是给监控 `curl -I` 用的（以前只挂 GET，首页 HEAD 会 405，2026-10-23 加）；
        GET 行为不变。
        """
        return _spa_response(full_path)


def _lan_ip() -> str | None:
    """本机在局域网里的地址，供手机上直接输入。

    用「往一个外网地址做 UDP connect」让内核挑出默认出口 IP —— 比遍历网卡
    少一堆特例（虚拟网卡、Docker、WSL 会各占一个，挑错了就是打不开）。
    不会真的发包：UDP 的 connect 只是选路由。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


if __name__ == "__main__":
    # 用 `python -m app.main` 启动，而不是敲 uvicorn 命令：host/port 从 .env 走，
    # 而且顺手把手机能用的地址打出来，省得每次自己查本机 IP。
    import uvicorn

    logger.info("本机打开：http://127.0.0.1:%d", settings.port)
    if settings.host not in ("127.0.0.1", "localhost"):
        lan = _lan_ip()
        logger.info(
            "手机打开：http://%s:%d（连同一个 WiFi）",
            lan or "<本机局域网IP>",
            settings.port,
        )
    # 传 app 对象而不是「模块:属性」字符串 —— 前者不会开 reload、也不会
    # 拉多个 worker，而调度器必须单进程（多 worker 会重复采集）
    uvicorn.run(app, host=settings.host, port=settings.port)
