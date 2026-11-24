import asyncio
import random

import httpx
import pytest
from sqlalchemy import func, select

import mock_amazon.main as mock
from mock_amazon.chaos import chaos
from mock_amazon.limiter import TokenBucket
from hub.models import Order, PlatformEvent
from hub.sync.backfill import backfill_orders
from hub.sync.client import AmazonClient
from hub.sync.events import handle_event
from hub.sync.limiter import RedisLimiter


@pytest.fixture(autouse=True)
def reset_mock():
    mock.ORDERS.clear()
    chaos.duplicate_rate = chaos.drop_rate = 0.0
    chaos.delay_ms = 0
    mock.PUSH_HOOK = None
    # 放宽模拟服务的限流，让测试跑得快；限流行为由别的测试覆盖
    for name in mock.BUCKETS:
        mock.BUCKETS[name] = TokenBucket(rate=10000, burst=10000)
    yield
    mock.PUSH_HOOK = None


def mock_client(redis_client):
    transport = httpx.ASGITransport(app=mock.app)
    limiter = RedisLimiter(redis_client, rate=10000, burst=10000)
    return AmazonClient("http://mock", limiter, transport=transport)


async def seed_orders(n):
    """向模拟服务造 n 个订单，每个订单状态变化 3 次，时间严格递增。"""
    transport = httpx.ASGITransport(app=mock.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        tasks = []
        for i in range(n):
            for step, status in enumerate(["Pending", "Paid", "Shipped"]):
                ts = f"2026-01-01T00:{step:02d}:{i % 60:02d}.{i:06d}+00:00"
                tasks.append(c.post("/_admin/orders", params={
                    "order_id": f"O{i}", "shop_id": "S1", "status": status, "ts": ts}))
        random.shuffle(tasks)  # 打乱发送顺序，制造真实乱序
        await asyncio.gather(*tasks)


async def final_state(sm):
    async with sm() as s:
        rows = (await s.execute(select(Order.order_id, Order.status))).all()
    return dict(rows)


async def test_backfill_recovers_when_all_events_lost(sessionmaker_, redis_client):
    """推送全部丢失，只靠增量补拉，订单也要齐全。"""
    chaos.drop_rate = 1.0
    await seed_orders(50)
    async with sessionmaker_() as s:
        n = await backfill_orders(s, mock_client(redis_client), "S1")
    assert n == 50
    got = await final_state(sessionmaker_)
    assert len(got) == 50


async def test_chaos_final_consistency(sessionmaker_, redis_client):
    """重复 30% + 丢失 20% + 乱序，事件加补拉之后，最终与源端完全一致。"""
    chaos.duplicate_rate = 0.3
    chaos.drop_rate = 0.2

    async def hook(event):
        async with sessionmaker_() as s:
            await handle_event(s, event)

    mock.PUSH_HOOK = hook
    N = 300
    await seed_orders(N)

    async with sessionmaker_() as s:
        await backfill_orders(s, mock_client(redis_client), "S1")

    got = await final_state(sessionmaker_)
    expected = {oid: o["status"] for oid, o in mock.ORDERS.items()}
    assert got == expected
    assert len(got) == N

    async with sessionmaker_() as s:
        events = (await s.execute(select(func.count()).select_from(PlatformEvent))).scalar_one()
    assert events <= N * 3
