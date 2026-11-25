"""S2 平台适配层测试：协议一致性、mock 不打成真实、真实接口明确未接入。"""

import httpx
import pytest

import mock_amazon.main as mock
from hub.adapters.base import (
    AdapterNotConfigured,
    AdapterNotImplemented,
    AdPlatformMode,
    AmazonAdsApiAdapter,
    AmazonSpApiAdapter,
    describe_mode,
    require_non_production,
)
from hub.adapters.local_ads import LocalAdsAdapter
from hub.adapters.mock_spapi import MockSpApiAdapter
from hub.agent.platform import InMemoryPlatform
from hub.sync.client import AmazonClient
from hub.sync.limiter import RedisLimiter


# ---------- LocalAdsAdapter ----------

async def test_local_adapter_delegates_and_keeps_behavior():
    platform = InMemoryPlatform({("S1", "kw1"): 1.0})
    adapter = LocalAdsAdapter(platform)
    assert adapter.mode is AdPlatformMode.LOCAL

    assert await adapter.get_bid("S1", "kw1") == 1.0
    await adapter.set_bid("S1", "kw1", 0.9, idempotency_key="k1")
    assert platform.bids[("S1", "kw1")] == 0.9
    assert len(adapter.calls) == 1                 # 原类的记录能力没被破坏
    assert len(platform.calls) == 1

    # 平台侧幂等：同一个 key 第二次不重复生效
    await adapter.set_bid("S1", "kw1", 0.8, idempotency_key="k1")
    assert platform.bids[("S1", "kw1")] == 0.9
    assert len(adapter.calls) == 2                 # 调用被记录，但状态没变


def test_require_non_production_blocks_production():
    require_non_production(AdPlatformMode.LOCAL)          # 不抛
    require_non_production(AdPlatformMode.MOCK)           # 不抛
    with pytest.raises(Exception) as e:
        require_non_production(AdPlatformMode.PRODUCTION)
    assert getattr(e.value, "code", "") == "production_write_blocked"


# ---------- 真实平台占位 ----------

def test_real_ads_adapter_requires_credentials():
    with pytest.raises(AdapterNotConfigured) as e:
        AmazonAdsApiAdapter(client_id="x")
    assert "refresh_token" in str(e.value)


def test_real_ads_adapter_not_implemented_even_with_credentials():
    with pytest.raises(AdapterNotImplemented):
        AmazonAdsApiAdapter(client_id="x", client_secret="y",
                            refresh_token="z", profile_id="p")


def test_real_spapi_adapter_requires_credentials():
    with pytest.raises(AdapterNotConfigured):
        AmazonSpApiAdapter()


def test_real_spapi_adapter_not_implemented_even_with_credentials():
    with pytest.raises(AdapterNotImplemented):
        AmazonSpApiAdapter(lwa_client_id="a", lwa_client_secret="b", refresh_token="c")


def test_describe_mode_never_claims_real_for_local_or_mock():
    for mode in (AdPlatformMode.LOCAL, AdPlatformMode.MOCK):
        text = describe_mode(mode)
        assert "不是真实" in text
    assert "真实授权" in describe_mode(AdPlatformMode.PRODUCTION)


# ---------- MockSpApiAdapter（包装 mock_amazon） ----------

@pytest.fixture
async def sp_adapter(redis_client, monkeypatch):
    mock.ORDERS.clear()
    monkeypatch.setattr(mock, "BUCKETS", {
        "orders": mock.TokenBucket(rate=10000, burst=10000),
        "order_detail": mock.TokenBucket(rate=10000, burst=10000),
        "inventory": mock.TokenBucket(rate=10000, burst=10000),
    })
    transport = httpx.ASGITransport(app=mock.app)
    limiter = RedisLimiter(redis_client, rate=10000, burst=10000)
    client = AmazonClient("http://mock", limiter, transport=transport)
    return MockSpApiAdapter(client)


async def test_mock_spapi_lists_orders(sp_adapter):
    transport = httpx.ASGITransport(app=mock.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        await c.post("/_admin/orders", params={
            "order_id": "O1", "shop_id": "S1", "status": "Paid",
            "ts": "2026-01-01T00:00:00+00:00"})

    orders = await sp_adapter.list_orders("S1")
    assert [o["order_id"] for o in orders] == ["O1"]
    assert sp_adapter.mode is AdPlatformMode.MOCK


async def test_mock_spapi_shop_isolation(sp_adapter):
    transport = httpx.ASGITransport(app=mock.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as c:
        await c.post("/_admin/orders", params={
            "order_id": "O1", "shop_id": "S2", "ts": "2026-01-01T00:00:00+00:00"})

    assert await sp_adapter.list_orders("S1") == []      # 看不到别的店


async def test_mock_spapi_inventory(sp_adapter):
    mock.INVENTORY["SKU1"] = 7
    assert (await sp_adapter.get_inventory("S1", "SKU1"))["quantity"] == 7


def test_mock_spapi_describe_is_honest():
    # describe 不依赖实例状态，直接用 __new__ 构造对象来调
    obj = MockSpApiAdapter.__new__(MockSpApiAdapter)
    d = obj.describe()
    assert d["mode"] == "mock"
    assert "不是 Amazon 真实" in d["note"]
