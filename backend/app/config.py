"""应用配置：优先读环境变量，其次读项目根目录 .env。"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "backend" / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- iFinD ---
    ifind_auth_token: str = ""
    ifind_base_url: str = "https://api-mcp.51ifind.com:8643/ds-mcp-servers"
    # 个人版权益上限 5 请求/秒，留 1 余量避免触发限流
    ifind_rate_limit: float = 4.0

    # --- akshare（仅用 push2ex 集群）---
    akshare_rate_limit: float = 1.0

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


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
