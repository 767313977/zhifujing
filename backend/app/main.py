"""FastAPI 入口。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import admin, limit, market, screener, watchlist
from app.config import ROOT, get_settings
from app.db import init_db

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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
    yield


app = FastAPI(title="复盘选股", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(market.router)
app.include_router(limit.router)
app.include_router(screener.router)
app.include_router(watchlist.router)
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
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
