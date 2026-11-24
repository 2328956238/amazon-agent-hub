import os

# 默认 SQLite 方便直接跑；换成 PostgreSQL 只需改环境变量：
# DATABASE_URL=postgresql+asyncpg://hub:hub@localhost:5432/hub
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./hub.db")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
AMAZON_BASE_URL = os.getenv("AMAZON_BASE_URL", "http://localhost:9000")
