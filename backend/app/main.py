"""FastAPI 入口。"""

import logging
import socket
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    admin,
    funds,
    limit,
    market,
    note,
    patterns,
    sector,
    stock,
    watchlist,
)
from app.config import ROOT, get_settings
from app.db import init_db
from app.jobs.scheduler import start_scheduler, stop_scheduler

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
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(title="致富经", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(market.router)
app.include_router(limit.router)
app.include_router(funds.router)
app.include_router(sector.router)
app.include_router(stock.router)
app.include_router(watchlist.router)
app.include_router(note.router)
app.include_router(patterns.router)
app.include_router(admin.router)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "db": str(settings.db_path),
        "ifind_token": bool(settings.ifind_auth_token),
        "frontend_built": FRONTEND_DIST.exists(),
    }


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        """单页应用回退：非 /api 路径一律交给前端路由。"""
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="接口不存在")
        # resolve 掉 ../ 后必须仍在 dist 目录内，否则 /..%2F..%2F.env 这类请求
        # 能穿越出 dist 读到项目根的 .env（含 iFinD token）甚至数据库文件。
        # 同文件的 /assets 用 StaticFiles 自带穿越防护，这里手写的必须自己校验。
        candidate = (FRONTEND_DIST / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(FRONTEND_DIST):
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")


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
