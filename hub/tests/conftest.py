import fakeredis.aioredis
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from hub.models import Base


@pytest_asyncio.fixture
async def engine(tmp_path):
    # 文件型 SQLite 才能让多个连接并发（内存库每个连接各一份）
    url = f"sqlite+aiosqlite:///{tmp_path}/t.db"
    eng = create_async_engine(url, connect_args={"timeout": 30})
    async with eng.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker_(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()
