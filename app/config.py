"""应用配置：全部通过环境变量注入，供 Compose / 本地运行共用。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://railway:railway@db:5432/railway_duty",
    )
    http_host: str = os.getenv("HTTP_HOST", "0.0.0.0")
    http_port: int = int(os.getenv("HTTP_PORT", "8000"))
    schema_path: str = os.getenv("SCHEMA_PATH", "/app/db/init.sql")


settings = Settings()
