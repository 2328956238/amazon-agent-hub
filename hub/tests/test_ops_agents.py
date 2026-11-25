"""只读运营 Agent 的调度测试：通过 LangGraph 真的路由到新 Agent。"""

import pytest

from hub import config
from hub.adapters.local_data import LocalDataAdapter
from hub.adapters.local_ads import LocalAdsAdapter
from hub.agent.llm import FakeLLM, LLMResponse
from hub.agent.platform import InMemoryPlatform
from hub.orchestration.agents.advertising import AdvertisingAgent, AdvertisingDeps
from hub.orchestration.agents.ops import (
    AdPerformanceAgent,
    InventoryAgent,
    OpsDeps,
    OrdersAgent,
    OverviewAgent,
    ProductAgent,
    extract_identifiers,
)
from hub.orchestration.graph import build_graph
from hub.orchestration.nodes import OrchestratorDeps
from hub.orchestration.registry import default_registry
from hub.orchestration.state import initial_state

DAY = "2026-01-10"


def make_deps(scenario=None, ads=None):
    data = LocalDataAdapter(config.AMAZON_DATA_DIR, scenario=scenario)
    ads = ads or LocalAdsAdapter(InMemoryPlatform({("S1", "kw1"): 1.0}))
    advertising = AdvertisingAgent(AdvertisingDeps(
        sessionmaker=None, ads=ads,
        llm_factory=lambda: FakeLLM([LLMResponse(kind="final", tokens=0, final={})]),
        today=lambda: DAY))
    ops = OpsDeps(data=data)
    registry = default_registry(advertising, ops_agents=[
        ProductAgent(ops), InventoryAgent(ops), OrdersAgent(ops),
        AdPerformanceAgent(ops), OverviewAgent(ops),
    ])
    return OrchestratorDeps(registry=registry, sessionmaker=None, ads=ads, model_mode="fake"), ads, data


async def run_message(message, shop="S1", thread="t-ops", scenario=None):
    deps, ads, data = make_deps(scenario=scenario)
    graph = build_graph(deps, allow_memory_fallback=True)
    state = initial_state(run_id="r", thread_id=thread, shop_id=shop, message=message)
    out = await graph.ainvoke(state, {"configurable": {"thread_id": thread}})
    return out["result"], ads


# ---------------------------------------------------------------------------
# 意图识别（新增三类）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("msg,intent_hint", [
    ("帮我查下 B0TEST0002 的库存", "inventory"),
    ("最近订单有哪些", "order"),
    ("广告表现怎么样", "ad_performance"),
    ("做个综合运营分析", "overview"),
    ("查一下 B0TEST0001 这个商品信息", "product"),
    ("广告花费太高，出价要降", "advertising"),      # 原有诊断意图不受影响
])
async def test_intent_routing(msg, intent_hint):
    result, _ = await run_message(msg, thread=f"t-{intent_hint}")
    assert result["intent"] == intent_hint
    assert result["agent"] == intent_hint or result["agent"] in {"orders"}


# ---------------------------------------------------------------------------
# 场景 A/B：库存查询、订单查询
# ---------------------------------------------------------------------------
async def test_scenario_a_inventory_by_asin():
    result, ads = await run_message("帮我查下 B0TEST0002 的库存", thread="a")
    assert result["status"] == "completed"
    payload = result["payload"]
    assert payload["source"] == "synthetic"
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["asin"] == "B0TEST0002" and item["available"] == 13
    assert ads.calls == []                                  # 只读，不写平台


async def test_scenario_b_recent_orders():
    result, _ = await run_message("最近订单有哪些", thread="b")
    payload = result["payload"]
    assert result["status"] == "completed"
    assert payload["count"] > 0
    assert payload["stats"]["order_count"] > 0
    assert all(o["shop_id"] == "S1" for o in payload["items"])


# ---------------------------------------------------------------------------
# 场景 C：库存风险
# ---------------------------------------------------------------------------
async def test_scenario_c_inventory_risk_normal():
    result, _ = await run_message("看看库存有没有缺货风险", thread="c1")
    payload = result["payload"]
    assert payload["mode"] == "risk"
    assert set(payload["summary"]) == {"critical", "warn", "ok"}
    assert payload["summary"]["critical"] == 0


async def test_scenario_c_inventory_risk_low_stock():
    result, _ = await run_message("看看库存有没有缺货风险", thread="c2", scenario="low_inventory")
    payload = result["payload"]
    assert payload["summary"]["critical"] >= 1
    critical = [i for i in payload["items"] if i["level"] == "critical"]
    assert critical[0]["sku"] == "SKU-CHARGER-65W"


# ---------------------------------------------------------------------------
# 场景 D：广告表现分析
# ---------------------------------------------------------------------------
async def test_scenario_d_ad_performance():
    result, _ = await run_message("看下广告表现", thread="d")
    payload = result["payload"]
    perf = payload["performance"]
    assert perf["source"] == "synthetic"
    assert perf["metrics"]["acos"] is not None
    assert perf["assessment"]["level"] in {"warn", "critical"}
    assert "domain/advertising.py" in payload["note"]


# ---------------------------------------------------------------------------
# 场景 E：综合运营分析（多数据源 + 多 Agent 能力）
# ---------------------------------------------------------------------------
async def test_scenario_e_overview():
    result, _ = await run_message("帮我做个综合运营分析", thread="e")
    payload = result["payload"]
    assert result["status"] == "completed"
    summary = payload["summary"]
    assert summary["products"] == 3
    assert summary["orders_7d"] > 0
    assert "inventory_critical" in summary and "ad_level" in summary
    assert payload["highlights"] and isinstance(payload["highlights"], list)
    assert payload["ad_performance"]["source"] == "synthetic"


# ---------------------------------------------------------------------------
# 未实现 / 错误处理
# ---------------------------------------------------------------------------
async def test_pricing_still_unimplemented():
    result, ads = await run_message("帮我把价格调低 10%", thread="p")
    assert result["status"] == "not_implemented"
    assert result["agent"] == "pricing" and result["agent_implemented"] is False
    assert ads.calls == []


async def test_missing_product_returns_failed():
    result, _ = await run_message("查一下 B0TEST9999 这个商品", thread="m")
    assert result["status"] == "failed"
    assert any("not_found" in e for e in result["errors"])


async def test_identifier_extraction():
    assert extract_identifiers("看下 B0TEST0001 的库存") == ("B0TEST0001", None)
    assert extract_identifiers("SKU-CHARGER-65W 还有多少") == (None, "SKU-CHARGER-65W")
    assert extract_identifiers("没有标识") == (None, None)


async def test_capabilities_reflect_ops_agents():
    deps, _, _ = make_deps()
    desc = {d["name"]: d for d in deps.registry.describe()}
    for name in ("product", "inventory", "orders", "ad_performance", "overview"):
        assert desc[name]["implemented"] is True, name
    assert desc["pricing"]["implemented"] is False


async def test_shop_isolation_between_agents():
    result, _ = await run_message("查一下 B0TEST0101 这个商品", shop="S1", thread="iso")
    assert result["status"] == "failed"           # S2 的 ASIN 在 S1 查不到
    assert any("not_found" in e for e in result["errors"])
