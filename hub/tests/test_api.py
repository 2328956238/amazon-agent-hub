"""S4 API 层测试：认证、店铺隔离、统一响应、未实现拒绝、审批恢复、503 降级。"""

import json

import httpx
import pytest

import hub.main as main_module
from hub.adapters.local_ads import LocalAdsAdapter
from hub.agent.llm import FakeLLM, LLMResponse
from hub.agent.platform import InMemoryPlatform
from hub.models import AdReport
from hub.orchestration.agents.advertising import AdvertisingAgent, AdvertisingDeps
from hub.orchestration.checkpointer import open_sqlite_checkpointer
from hub.orchestration.graph import build_graph
from hub.orchestration.nodes import OrchestratorDeps
from hub.orchestration.registry import default_registry
from hub.api.agent import Orchestrator

DAY = "2026-01-10"
DAYS = ("2026-01-08", "2026-01-09", "2026-01-10")
TOK_S1 = "tok-s1-admin"
TOK_S2 = "tok-s2-operator"


def make_llm():
    return FakeLLM([
        LLMResponse(kind="tool_call", tool="get_ad_summary", args={"days": 3}, tokens=80),
        LLMResponse(kind="final", tokens=120, final={"suggestions": [{
            "action": "adjust_bid", "target": "kw1", "before": 1.0, "after": 0.7,
            "reason": "花费高且无订单",
            "evidence": [{"call_id": "c1", "keyword_id": "kw1", "field": "spend", "value": 52.2}],
        }]}),
    ])


async def seed_ads(sm, shop="S1"):
    async with sm() as s:
        async with s.begin():
            for d in DAYS:
                s.add(AdReport(shop_id=shop, day=d, keyword_id="kw1", keyword="phone case",
                               spend=17.4, clicks=27, orders=0))


async def seed_other_shop(sm):
    async with sm() as s:
        async with s.begin():
            for d in DAYS:
                s.add(AdReport(shop_id="S2", day=d, keyword_id="kw1", keyword="other",
                               spend=333.0, clicks=500, orders=1))


@pytest.fixture
async def api(sessionmaker_, tmp_path, monkeypatch):
    await seed_ads(sessionmaker_)
    await seed_other_shop(sessionmaker_)
    monkeypatch.setenv("API_TOKENS", json.dumps({
        TOK_S1: {"shop_id": "S1", "actor": "alice", "role": "admin"},
        TOK_S2: {"shop_id": "S2", "actor": "bob", "role": "operator"},
    }))

    platform = InMemoryPlatform({("S1", "kw1"): 1.0, ("S2", "kw1"): 2.0})
    ads = LocalAdsAdapter(platform)
    deps = OrchestratorDeps(
        registry=default_registry(AdvertisingAgent(AdvertisingDeps(
            sessionmaker=sessionmaker_, ads=ads, llm_factory=make_llm, today=lambda: DAY))),
        sessionmaker=sessionmaker_, ads=ads, model_mode="fake",
    )

    async with open_sqlite_checkpointer(str(tmp_path / "cp.sqlite")) as saver:
        main_module.app.state.orchestrator = Orchestrator(
            graph=build_graph(deps, checkpointer=saver), deps=deps)
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, ads
    main_module.app.state.orchestrator = None


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------------------
# 认证与参数
# ---------------------------------------------------------------------------
async def test_missing_token_is_401(api):
    client, _ = api
    r = await client.post("/api/v1/agent/chat", json={"message": "广告花费太高"})
    assert r.status_code == 401 and r.json()["ok"] is False
    assert r.json()["error"]["code"] == "unauthorized"


async def test_invalid_token_is_401(api):
    client, _ = api
    r = await client.post("/api/v1/agent/chat", json={"message": "广告"},
                          headers=auth("nope"))
    assert r.status_code == 401


async def test_client_cannot_pass_shop_id(api):
    client, _ = api
    r = await client.post("/api/v1/agent/chat",
                          json={"message": "广告花费太高", "shop_id": "S2"},
                          headers=auth(TOK_S1))
    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False and body["error"]["code"] == "validation_error"
    assert "shop_id" in json.dumps(body, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 能力清单 / 未实现
# ---------------------------------------------------------------------------
async def test_capabilities_distinguishes_implemented(api):
    client, _ = api
    r = await client.get("/api/v1/agent/capabilities", headers=auth(TOK_S1))
    data = r.json()["data"]
    impl = {a["name"]: a["implemented"] for a in data["agents"]}
    assert impl["advertising"] is True
    assert impl["inventory"] is False and impl["pricing"] is False
    assert data["model_mode"] == "fake"
    assert data["ads_mode"] == "local"


async def test_unimplemented_agent_returns_explicit_status(api):
    client, ads = api
    r = await client.post("/api/v1/agent/chat", json={"message": "库存快没了，要补货"},
                          headers=auth(TOK_S1))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "not_implemented"
    assert data["agent"] == "inventory"
    assert data["agent_implemented"] is False
    assert "尚未实现" in data["payload"]["message"]
    assert ads.calls == []


async def test_unknown_intent_fails_gracefully(api):
    client, _ = api
    r = await client.post("/api/v1/agent/chat", json={"message": "今天天气不错"},
                          headers=auth(TOK_S1))
    data = r.json()["data"]
    assert data["status"] == "failed"
    assert data["errors"]


# ---------------------------------------------------------------------------
# 广告链路 + 审批恢复
# ---------------------------------------------------------------------------
async def test_advertising_run_waits_for_approval_without_writing(api):
    client, ads = api
    r = await client.post("/api/v1/agent/chat",
                          json={"message": "广告花费太高，关键词出价要降"},
                          headers=auth(TOK_S1))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "awaiting_approval"
    assert data["shop_id"] == "S1"
    assert data["agent"] == "advertising"
    assert data["pending_approvals"]
    assert ads.calls == []                       # 关键：没审批就不写平台
    assert data["run_id"] and data["thread_id"]


async def test_approve_then_execute_via_api(api):
    client, ads = api
    r1 = await client.post("/api/v1/agent/chat",
                           json={"message": "广告花费太高，关键词出价要降"},
                           headers=auth(TOK_S1))
    data = r1.json()["data"]
    sid = data["pending_approvals"][0]["suggestion_id"]

    r2 = await client.post("/api/v1/agent/chat", json={
        "message": "同意执行",
        "thread_id": data["thread_id"],
        "approval": [{"suggestion_id": sid, "decision": "approve"}],
    }, headers=auth(TOK_S1))
    out = r2.json()["data"]
    assert out["status"] == "completed"
    assert out["approval_results"][0]["result"] == "executed"
    assert len(ads.calls) == 1
    assert ads.bids[("S1", "kw1")] == 0.7


async def test_reject_via_api_does_not_write(api):
    client, ads = api
    r1 = await client.post("/api/v1/agent/chat",
                           json={"message": "广告花费太高，关键词出价要降"},
                           headers=auth(TOK_S1))
    data = r1.json()["data"]
    sid = data["pending_approvals"][0]["suggestion_id"]

    r2 = await client.post("/api/v1/agent/chat", json={
        "message": "拒绝", "thread_id": data["thread_id"],
        "approval": [{"suggestion_id": sid, "decision": "reject"}],
    }, headers=auth(TOK_S1))
    out = r2.json()["data"]
    assert out["status"] == "completed"
    assert ads.calls == []


async def test_resume_requires_thread_id(api):
    client, _ = api
    r = await client.post("/api/v1/agent/chat", json={
        "message": "同意",
        "approval": [{"suggestion_id": "x", "decision": "approve"}],
    }, headers=auth(TOK_S1))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "thread_id_required"


# ---------------------------------------------------------------------------
# 运行查询与店铺隔离
# ---------------------------------------------------------------------------
async def test_get_run_returns_snapshot(api):
    client, _ = api
    r1 = await client.post("/api/v1/agent/chat",
                           json={"message": "广告花费太高，关键词出价要降"},
                           headers=auth(TOK_S1))
    tid = r1.json()["data"]["thread_id"]
    r2 = await client.get(f"/api/v1/agent/runs/{tid}", headers=auth(TOK_S1))
    assert r2.status_code == 200
    assert r2.json()["data"]["status"] == "awaiting_approval"
    assert r2.json()["data"]["next_nodes"] == ["approval_gate"]


async def test_get_unknown_run_is_404(api):
    client, _ = api
    r = await client.get("/api/v1/agent/runs/does-not-exist", headers=auth(TOK_S1))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "run_not_found"


async def test_other_shop_cannot_read_run(api):
    client, _ = api
    r1 = await client.post("/api/v1/agent/chat",
                           json={"message": "广告花费太高，关键词出价要降"},
                           headers=auth(TOK_S1))
    tid = r1.json()["data"]["thread_id"]
    r2 = await client.get(f"/api/v1/agent/runs/{tid}", headers=auth(TOK_S2))
    assert r2.status_code == 404                    # 不泄露别的店有这条记录


async def test_shop_isolation_in_agent_run(api):
    """S2 的 token 只能看到 S2 的数据：S1 的证据匹配不上，建议被丢弃。"""
    client, ads = api
    r = await client.post("/api/v1/agent/chat",
                          json={"message": "广告花费太高，关键词出价要降"},
                          headers=auth(TOK_S2))
    data = r.json()["data"]
    assert data["shop_id"] == "S2"
    assert data["payload"]["dropped_no_evidence"] == 1
    assert data["payload"]["suggestions"] == []
    assert ads.calls == []


# ---------------------------------------------------------------------------
# 降级：编排层不可用不能影响 webhook
# ---------------------------------------------------------------------------
async def test_orchestrator_unavailable_returns_503(sessionmaker_, monkeypatch):
    from hub.db import get_session

    monkeypatch.setenv("API_TOKENS", json.dumps({
        TOK_S1: {"shop_id": "S1", "actor": "alice", "role": "admin"},
    }))
    main_module.app.state.orchestrator = None
    main_module.app.state.orchestrator_error = "ImportError: langgraph missing"

    async def _session_override():
        async with sessionmaker_() as s:
            yield s

    main_module.app.dependency_overrides[get_session] = _session_override
    try:
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/v1/agent/chat", json={"message": "广告"},
                                  headers=auth(TOK_S1))
            assert r.status_code == 503
            assert r.json()["error"]["code"] == "orchestrator_unavailable"

            # webhook 不受影响（仍然能接收事件）
            w = await client.post("/webhook/amazon", json={
                "event_id": "E-503", "shop_id": "S1", "type": "ORDER_CHANGE",
                "order_id": "O-503", "status": "Pending", "event_time": "2026-01-01T00:00:00+00:00"})
            assert w.status_code == 200
            assert w.json()["status"] == "ok"
    finally:
        main_module.app.dependency_overrides.pop(get_session, None)
