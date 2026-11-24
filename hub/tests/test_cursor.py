import httpx
import mock_amazon.main as mock
from mock_amazon.chaos import chaos
from mock_amazon.limiter import TokenBucket
from hub.sync.backfill import backfill_orders
from hub.tests.test_chaos import mock_client, final_state


async def put(order_id, status, ts):
    t = httpx.ASGITransport(app=mock.app)
    async with httpx.AsyncClient(transport=t, base_url="http://mock") as c:
        await c.post("/_admin/orders", params={
            "order_id": order_id, "shop_id": "S1", "status": status, "ts": ts})


async def test_incremental_backfill_uses_cursor(sessionmaker_, redis_client):
    """第二次补拉只拉增量，条数应远小于总数，且最终一致。"""
    mock.ORDERS.clear()
    for name in mock.BUCKETS:
        mock.BUCKETS[name] = TokenBucket(rate=10000, burst=10000)
    chaos.drop_rate = 1.0          # 事件全丢，只靠补拉
    client = mock_client(redis_client)

    for i in range(100):
        await put(f"O{i}", "Pending", f"2026-01-01T00:{(i * 12) // 60:02d}:{(i * 12) % 60:02d}.{i:06d}+00:00")
    async with sessionmaker_() as s:
        first = await backfill_orders(s, client, "S1")
    assert first == 100

    # 过了很久（超出重叠窗口），只有 3 个订单有新变化
    for i in (3, 50, 77):
        await put(f"O{i}", "Shipped", f"2026-01-01T01:00:00.{i:06d}+00:00")
    async with sessionmaker_() as s:
        second = await backfill_orders(s, client, "S1")
    # 重叠窗口(5 分钟)会重复拉回窗口内的旧订单，这是为防漏数据付出的代价，
    # 重复由幂等写入吸收。所以增量应远小于总数，而不是恰好等于变化数。
    assert 3 <= second <= 35, f"增量拉取量异常: {second}"
    assert second < first / 2

    got = await final_state(sessionmaker_)
    expected = {k: v["status"] for k, v in mock.ORDERS.items()}
    assert got == expected
