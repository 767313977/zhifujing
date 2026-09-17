"""FastAPI 入口。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import init_db

settings = get_settings()

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    logger.info("数据库就绪: %s", settings.db_path)
    if not settings.ifind_auth_token:
        logger.warning("未配置 IFIND_AUTH_TOKEN，iFinD 相关采集将失败")
    yield


app = FastAPI(title="复盘选股", version="0.1.0", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "db": str(settings.db_path),
        "ifind_token": bool(settings.ifind_auth_token),
    }
