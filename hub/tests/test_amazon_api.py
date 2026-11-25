"""本地数据 API 测试：认证、店铺隔离、过滤、来源标记、错误处理。"""

import json

import httpx
import pytest

import hub.main as main_module
from hub import config
from hub.adapters.local_data import LocalDataAdapter
from hub.adapters.local_ads import LocalAdsAdapter
from hub.agent.llm import FakeLLM, LLMResponse
from hub.agent.platform import InMemoryPlatform
from hub.api.agent import Orchestrator
from hub.orchestration.agents.advertising import AdvertisingAgent, AdvertisingDeps
from hub.orchestration.agents.ops import (
    AdPerformanceAgent,
    InventoryAgent,
    OpsDeps,
    OrdersAgent,
    OverviewAgent,
    ProductAgent,
)
from hub.orchestration.checkpointer import open_sqlite_checkpointer
from hub.orchestration.graph import build_graph
from hub.orchestration.nodes import OrchestratorDeps
from hub.orchestration.registry import default_registry

TOK_S1 = "tok-amz-s1"
TOK_S2 = "tok-amz-s2"


def make_deps(data):
    ads = LocalAdsAdapter(InMemoryPlatform({("S1", "kw1"): 1.0, ("S2", "kw1"): 1.0}))
    advertising = AdvertisingAgent(AdvertisingDeps(
        sessionmaker=None, ads=ads,
        llm_factory=lambda: FakeLLM([LLMResponse(kind="final", tokens=0, final={})]),
        today=lambda: "2026-01-10"))
    ops = OpsDeps(data=data)
    registry = default_registry(advertising, ops_agents=[
        ProductAgent(ops), InventoryAgent(ops), OrdersAgent(ops),
        AdPerformanceAgent(ops), OverviewAgent(ops)])
    return OrchestratorDeps(registry=registry, sessionmaker=None, ads=ads, model_mode="fake")


@pytest.fixture
async def api(sessionmaker_, tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKENS", json.dumps({
        TOK_S1: {"shop_id": "S1", "actor": "alice", "role": "admin"},
        TOK_S2: {"shop_id": "S2", "actor": "bob", "role": "operator"},
    }))
    data = LocalDataAdapter(config.AMAZON_DATA_DIR)
    deps = make_deps(data)
    async with open_sqlite_checkpointer(str(tmp_path / "cp.sqlite")) as saver:
        main_module.app.state.local_data = data
        main_module.app.state.orchestrator = Orchestrator(
            graph=build_graph(deps, checkpointer=saver), deps=deps)
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    main_module.app.state.local_data = None
    main_module.app.state.orchestrator = None


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


ENDPOINTS = ["/api/v1/amazon/products", "/api/v1/amazon/orders",
             "/api/v1/amazon/inventory", "/api/v1/amazon/advertising",
             "/api/v1/amazon/summary"]


@pytest.mark.parametrize("path", ENDPOINTS)
async def test_requires_auth(api, path):
    r = await api.get(path)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


async def test_products_shape_and_source(api):
    r = await api.get("/api/v1/amazon/products", headers=auth(TOK_S1))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["shop_id"] == "S1" and data["source"] == "synthetic"
    assert "非真实卖家数据" in data["note"]
    assert data["count"] == 3
    assert {p["asin"] for p in data["items"]} == {"B0TEST0001", "B0TEST0002", "B0TEST0003"}


async def test_products_shop_isolation(api):
    s1 = (await api.get("/api/v1/amazon/products", headers=auth(TOK_S1))).json()["data"]
    s2 = (await api.get("/api/v1/amazon/products", headers=auth(TOK_S2))).json()["data"]
    assert s2["count"] == 2
    assert not ({p["asin"] for p in s1["items"]} & {p["asin"] for p in s2["items"]})
    assert all(p["shop_id"] == "S2" for p in s2["items"])


async def test_product_filter_and_404(api):
    ok = await api.get("/api/v1/amazon/products?asin=B0TEST0002", headers=auth(TOK_S1))
    assert ok.json()["data"]["count"] == 1
    missing = await api.get("/api/v1/amazon/products?asin=B0TEST9999", headers=auth(TOK_S1))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def test_orders_filter_limit_and_stats(api):
    r = await api.get("/api/v1/amazon/orders?limit=2", headers=auth(TOK_S1))
    data = r.json()["data"]
    assert data["count"] <= 2 and data["stats"]["order_count"] > 0

    canceled = await api.get("/api/v1/amazon/orders?status=Canceled&limit=50",
                             headers=auth(TOK_S1))
    assert canceled.json()["data"]["items"]
    assert all(o["status"] == "Canceled" for o in canceled.json()["data"]["items"])


async def test_orders_invalid_limit_is_422(api):
    r = await api.get("/api/v1/amazon/orders?limit=0", headers=auth(TOK_S1))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_inventory_derived_available_and_risk(api):
    r = await api.get("/api/v1/amazon/inventory", headers=auth(TOK_S1))
    data = r.json()["data"]
    assert data["count"] == 3
    for item in data["items"]:
        assert item["available"] == item["on_hand"] - item["reserved"]
    assert len(data["risk"]) == 3
    assert {r["level"] for r in data["risk"]} <= {"ok", "warn", "critical"}

    one = await api.get("/api/v1/amazon/inventory?sku=SKU-CHARGER-65W", headers=auth(TOK_S1))
    assert one.json()["data"]["count"] == 1

    missing = await api.get("/api/v1/amazon/inventory?sku=SKU-NOPE", headers=auth(TOK_S1))
    assert missing.status_code == 404


async def test_advertising_performance(api):
    r = await api.get("/api/v1/amazon/advertising", headers=auth(TOK_S1))
    data = r.json()["data"]
    assert data["count"] == 3
    perf = data["performance"]
    assert perf["source"] == "synthetic"
    assert perf["metrics"]["acos"] is not None
    assert perf["assessment"]["level"] in {"ok", "warn", "critical"}


async def test_summary(api):
    r = await api.get("/api/v1/amazon/summary", headers=auth(TOK_S1))
    summary = r.json()["data"]["summary"]
    assert summary["products"] == 3
    assert summary["orders_7d"] > 0
    assert "inventory_critical" in summary and "ad_level" in summary


async def test_data_unavailable_returns_generic_503(api):
    main_module.app.state.local_data = None
    r = await api.get("/api/v1/amazon/products", headers=auth(TOK_S1))
    assert r.status_code == 503
    body = r.text.lower()
    assert r.json()["error"]["code"] == "data_unavailable"
    for leaked in ("/", "data/amazon", "jsondecodeerror", "traceback"):
        assert leaked not in body
