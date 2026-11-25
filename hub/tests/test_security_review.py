"""第二轮独立审查的验证测试：安全问题、审批幂等、跨进程恢复、InMemorySaver 回退。

这一组测试按「期望的安全行为」编写；如果实现有漏洞，测试会失败并指出位置。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

import hub.main as main_module
from hub.adapters.local_ads import LocalAdsAdapter
from hub.agent.llm import FakeLLM, LLMResponse
from hub.agent.platform import InMemoryPlatform
from hub.api.agent import Orchestrator
from hub.models import AdReport, DiagnosisRun, Suggestion
from hub.orchestration.agents.advertising import AdvertisingAgent, AdvertisingDeps
from hub.orchestration.checkpointer import open_sqlite_checkpointer
from hub.orchestration.graph import OrchestratorUnavailable, build_graph
from hub.orchestration.nodes import OrchestratorDeps
from hub.orchestration.registry import default_registry

DAY = "2026-01-10"
DAYS = ("2026-01-08", "2026-01-09", "2026-01-10")
TOK_S1 = "tok-s1-admin"
TOK_S2 = "tok-s2-operator"
TOK_S2_ADMIN = "tok-s2-admin"


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


@pytest.fixture
async def api(sessionmaker_, tmp_path, monkeypatch):
    await seed_ads(sessionmaker_, "S1")
    await seed_ads(sessionmaker_, "S2")
    monkeypatch.setenv("API_TOKENS", json.dumps({
        TOK_S1: {"shop_id": "S1", "actor": "alice", "role": "admin"},
        TOK_S2: {"shop_id": "S2", "actor": "bob", "role": "operator"},
        TOK_S2_ADMIN: {"shop_id": "S2", "actor": "carol", "role": "admin"},
    }))

    platform = InMemoryPlatform({("S1", "kw1"): 1.0, ("S2", "kw1"): 1.0})
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
            yield client, ads, sessionmaker_
    main_module.app.state.orchestrator = None


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


async def start_run(client, tok=TOK_S1):
    r = await client.post("/api/v1/agent/chat",
                          json={"message": "广告花费太高，关键词出价要降"}, headers=auth(tok))
    assert r.status_code == 200, r.text
    return r.json()["data"]


# ---------------------------------------------------------------------------
# 越权：跨店铺 thread_id
# ---------------------------------------------------------------------------
async def test_other_shop_cannot_resume_foreign_thread(api):
    """S2 拿 S1 的 thread_id 去审批 → 必须拒绝，且不能产生写操作。"""
    client, ads, _ = api
    data = await start_run(client, TOK_S1)
    tid = data["thread_id"]
    sid = data["pending_approvals"][0]["suggestion_id"]

    r = await client.post("/api/v1/agent/chat", json={
        "message": "批准",
        "thread_id": tid,
        "approval": [{"suggestion_id": sid, "decision": "approve"}],
    }, headers=auth(TOK_S2_ADMIN))
    assert r.status_code in (403, 404), f"跨店审批竟然返回 {r.status_code}: {r.text}"
    assert ads.calls == []


async def test_other_shop_cannot_inject_state_into_foreign_thread(api):
    """S2 用 S1 的 thread_id 发起新请求 → 必须拒绝，且 S1 的运行状态不能被污染。"""
    client, ads, _ = api
    data = await start_run(client, TOK_S1)
    tid = data["thread_id"]

    r = await client.post("/api/v1/agent/chat",
                          json={"message": "广告花费太高", "thread_id": tid},
                          headers=auth(TOK_S2))
    assert r.status_code in (403, 404), f"跨店复用 thread 竟然返回 {r.status_code}: {r.text}"

    r2 = await client.get(f"/api/v1/agent/runs/{tid}", headers=auth(TOK_S1))
    assert r2.status_code == 200
    assert r2.json()["data"]["shop_id"] == "S1"
    assert r2.json()["data"]["pending_approvals"]          # 原待审批还在
    assert ads.calls == []


async def test_other_shop_cannot_approve_suggestion_by_id(api):
    """S2 即使拿到 S1 的 suggestion_id（其他店铺接口泄露等），也不能审批。"""
    client, ads, _ = api
    data = await start_run(client, TOK_S1)
    sid = data["pending_approvals"][0]["suggestion_id"]

    # S2 自己发起一次运行，然后用自己的 thread_id + S1 的 suggestion_id 去审批
    own = await start_run(client, TOK_S2)
    r = await client.post("/api/v1/agent/chat", json={
        "message": "批准",
        "thread_id": own["thread_id"],
        "approval": [{"suggestion_id": sid, "decision": "approve"}],
    }, headers=auth(TOK_S2_ADMIN))
    assert r.status_code == 200                      # 请求本身合法
    assert ads.calls == []                           # 但跨店建议不能被执行
    # S1 的建议仍然是待审批
    r2 = await client.get(f"/api/v1/agent/runs/{data['thread_id']}", headers=auth(TOK_S1))
    assert r2.json()["data"]["pending_approvals"]


# ---------------------------------------------------------------------------
# 审批幂等与恢复
# ---------------------------------------------------------------------------
async def test_duplicate_approval_does_not_write_twice(api):
    client, ads, sm = api
    data = await start_run(client)
    tid = data["thread_id"]
    sid = data["pending_approvals"][0]["suggestion_id"]
    body = {"message": "批准", "thread_id": tid,
            "approval": [{"suggestion_id": sid, "decision": "approve"}]}

    r1 = await client.post("/api/v1/agent/chat", json=body, headers=auth(TOK_S1))
    assert r1.status_code == 200 and r1.json()["data"]["status"] == "completed"
    assert len(ads.calls) == 1

    r2 = await client.post("/api/v1/agent/chat", json=body, headers=auth(TOK_S1))
    assert r2.status_code == 200, r2.text
    assert r2.json()["data"]["status"] == "completed"   # 幂等：返回已完成，不重复执行
    assert len(ads.calls) == 1, "重复审批导致平台被写了两次"

    async with sm() as s:
        st = (await s.execute(select(Suggestion.status).where(Suggestion.id == sid))).scalar_one()
    assert st == "EXECUTED"


async def test_resume_does_not_rerun_diagnosis(api):
    client, ads, sm = api
    data = await start_run(client)
    sid = data["pending_approvals"][0]["suggestion_id"]

    await client.post("/api/v1/agent/chat", json={
        "message": "批准", "thread_id": data["thread_id"],
        "approval": [{"suggestion_id": sid, "decision": "approve"}]}, headers=auth(TOK_S1))

    async with sm() as s:
        runs = (await s.execute(select(func.count()).select_from(DiagnosisRun))).scalar_one()
    assert runs == 1, f"恢复时重复执行了诊断，DiagnosisRun={runs}"


async def test_operator_cannot_forge_admin_role_in_approval(api):
    """S2 的 operator token 不能通过请求体自称 admin 来批准高风险建议（越权审批）。"""
    client, ads, _ = api
    data = await start_run(client, TOK_S2)            # S2 的 kw1 也调 30% → high 风险
    assert data["status"] == "awaiting_approval"
    sid = data["pending_approvals"][0]["suggestion_id"]

    r = await client.post("/api/v1/agent/chat", json={
        "message": "批准",
        "thread_id": data["thread_id"],
        "approval": [{"suggestion_id": sid, "decision": "approve",
                      "actor": "hacker", "role": "admin"}],
    }, headers=auth(TOK_S2))                          # operator，不是 admin

    if r.status_code == 422:
        # 更好：请求体根本不允许 actor/role
        assert ads.calls == []
        return
    assert r.status_code == 200
    assert ads.calls == [], "operator 伪造 role=admin 竟然执行了写操作"


async def test_operator_cannot_approve_high_risk_even_without_forging(api):
    """合规的 operator 也不行：高风险建议只有 admin 能批（approval.py 的角色门禁要透传到 API）。"""
    client, ads, _ = api
    data = await start_run(client, TOK_S2)
    sid = data["pending_approvals"][0]["suggestion_id"]
    r = await client.post("/api/v1/agent/chat", json={
        "message": "批准", "thread_id": data["thread_id"],
        "approval": [{"suggestion_id": sid, "decision": "approve"}],
    }, headers=auth(TOK_S2))                         # operator
    assert r.status_code == 200
    assert ads.calls == []
    assert r.json()["data"]["approval_results"][0]["result"] == "forbidden"


async def test_unauthenticated_capabilities_is_401(api):
    client, _, _ = api
    r = await client.get("/api/v1/agent/capabilities")
    assert r.status_code == 401


async def test_503_response_does_not_leak_internal_details(api):
    """服务不可用时只给通用文案；路径/依赖/凭证等只进服务端日志。"""
    client, _, _ = api
    main_module.app.state.orchestrator = None
    main_module.app.state.orchestrator_error = (
        "CheckpointUnavailable: /home/secret_user/keys/hub_checkpoints.sqlite "
        "token=abc123 db=postgresql://user:pass@db:5432/hub")

    r = await client.post("/api/v1/agent/chat", json={"message": "广告"},
                          headers=auth(TOK_S1))
    assert r.status_code == 503
    body = r.text.lower()
    assert r.json()["error"]["code"] == "orchestrator_unavailable"
    for leaked in ("secret_user", "abc123", "postgresql://", "/home/"):
        assert leaked not in body, f"503 泄露了内部信息: {leaked}"


async def test_500_response_does_not_leak_internal_exception(api, monkeypatch):
    client, _, _ = api
    orch = main_module.app.state.orchestrator
    assert orch is not None

    async def boom(**_kwargs):
        raise RuntimeError("SECRET=abc123 db=postgresql://user:pass@db:5432/hub")

    monkeypatch.setattr(orch, "start", boom)
    r = await client.post("/api/v1/agent/chat", json={"message": "广告花费太高"},
                          headers=auth(TOK_S1))
    assert r.status_code == 500
    body = r.text.lower()
    assert r.json()["error"]["code"] == "agent_run_failed"
    for leaked in ("secret", "abc123", "postgresql://", "runtimeerror"):
        assert leaked not in body, f"500 泄露了内部信息: {leaked}"


async def test_concurrent_runs_share_one_checkpointer_safely(api):
    """同一个 SQLite checkpointer 被多个并发请求使用时，不能报错、不能串店。"""
    import asyncio

    client, ads, _ = api

    async def one(i):
        return await client.post("/api/v1/agent/chat", json={
            "message": "广告花费太高，关键词出价要降", "thread_id": f"conc-{i}"},
            headers=auth(TOK_S1))

    results = await asyncio.gather(*[one(i) for i in range(5)])
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    tids = {r.json()["data"]["thread_id"] for r in results}
    assert len(tids) == 5                                  # 五条运行互不覆盖
    assert ads.calls == []                                  # 都没审批，都没写


# ---------------------------------------------------------------------------
# InMemorySaver 回退：正式路径不得使用
# ---------------------------------------------------------------------------
def test_build_graph_refuses_memory_saver_by_default():
    """不传 checkpointer 且未显式允许时，必须直接失败，避免生产误用内存存储。"""
    deps = OrchestratorDeps(registry=default_registry(AdvertisingAgent(
        AdvertisingDeps(None, None, make_llm, lambda: DAY))))
    with pytest.raises(OrchestratorUnavailable):
        build_graph(deps)


def test_build_graph_allows_memory_saver_when_explicitly_requested():
    deps = OrchestratorDeps(registry=default_registry(AdvertisingAgent(
        AdvertisingDeps(None, None, make_llm, lambda: DAY))))
    graph = build_graph(deps, allow_memory_fallback=True)
    assert graph is not None


# ---------------------------------------------------------------------------
# 真正的进程重启：子进程读取并恢复父进程写的 checkpoint
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 生产环境防护
# ---------------------------------------------------------------------------
async def test_production_adapter_write_is_blocked(sessionmaker_):
    """即使审批通过，production 适配器也必须被编排层拦住（当前阶段默认只读）。"""
    from langgraph.types import Command
    from hub.adapters.base import AdPlatformMode
    from hub.orchestration.state import initial_state

    await seed_ads(sessionmaker_, "S1")

    class ProdAds:
        mode = AdPlatformMode.PRODUCTION

        def __init__(self, inner):
            self.inner = inner

        async def get_bid(self, shop_id, keyword_id):
            return await self.inner.get_bid(shop_id, keyword_id)

        async def set_bid(self, shop_id, keyword_id, value, idempotency_key):
            return await self.inner.set_bid(shop_id, keyword_id, value, idempotency_key)

    inner = InMemoryPlatform({("S1", "kw1"): 1.0})
    ads = ProdAds(inner)
    deps = OrchestratorDeps(
        registry=default_registry(AdvertisingAgent(AdvertisingDeps(
            sessionmaker=sessionmaker_, ads=ads, llm_factory=make_llm, today=lambda: DAY))),
        sessionmaker=sessionmaker_, ads=ads, model_mode="fake")
    graph = build_graph(deps, allow_memory_fallback=True)
    cfg = {"configurable": {"thread_id": "prod-t"}}
    await graph.ainvoke(initial_state(run_id="r", thread_id="prod-t", shop_id="S1",
                                      message="广告花费太高，关键词出价要降"), cfg)
    sid = (await graph.aget_state(cfg)).values["pending_approvals"][0]["suggestion_id"]
    out = await graph.ainvoke(Command(resume={"items": [{
        "suggestion_id": sid, "decision": "approve", "actor": "a", "role": "admin"}]}), cfg)

    assert inner.calls == []                                   # 生产环境一次都没写
    assert out["result"]["status"] == "failed"
    assert any("生产" in e or "production" in e for e in out["result"]["errors"])


def test_production_env_refuses_builtin_dev_token(monkeypatch):
    """APP_ENV=production 且没配 API_TOKENS 时必须 fail-closed，不能让 dev-token 当管理员。"""
    from fastapi import HTTPException
    from hub.api.agent import _token_map

    monkeypatch.delenv("API_TOKENS", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEFAULT_API_TOKEN", "dev-token")

    with pytest.raises(HTTPException) as exc:
        _token_map()
    assert exc.value.status_code == 500
    assert exc.value.detail["code"] == "auth_not_configured"


# ---------------------------------------------------------------------------
# 真正的进程重启：子进程读取并恢复父进程写的 checkpoint
# ---------------------------------------------------------------------------
CHILD = r'''
import asyncio, os, sys
from langgraph.types import Command
from hub.adapters.local_ads import LocalAdsAdapter
from hub.agent.llm import FakeLLM, LLMResponse
from hub.agent.platform import InMemoryPlatform
from hub.db import SessionLocal
from hub.orchestration.agents.advertising import AdvertisingAgent, AdvertisingDeps
from hub.orchestration.checkpointer import open_sqlite_checkpointer
from hub.orchestration.graph import build_graph
from hub.orchestration.nodes import OrchestratorDeps
from hub.orchestration.registry import default_registry

tid, sid = sys.argv[1], sys.argv[2]

async def main():
    ads = LocalAdsAdapter(InMemoryPlatform({("S1", "kw1"): 1.0}))
    deps = OrchestratorDeps(
        registry=default_registry(AdvertisingAgent(AdvertisingDeps(
            sessionmaker=SessionLocal, ads=ads,
            llm_factory=lambda: FakeLLM([LLMResponse(kind="final", tokens=0, final={})]),
            today=lambda: "2026-01-10"))),
        sessionmaker=SessionLocal, ads=ads, model_mode="fake")
    async with open_sqlite_checkpointer(os.environ["GRAPH_CHECKPOINT_DB"]) as saver:
        graph = build_graph(deps, checkpointer=saver)
        cfg = {"configurable": {"thread_id": tid}}
        snap = await graph.aget_state(cfg)
        print("CHILD_SHOP", snap.values.get("shop_id"))
        print("CHILD_PENDING", len(snap.values.get("pending_approvals") or []))
        out = await graph.ainvoke(Command(resume={"items": [{
            "suggestion_id": sid, "decision": "approve", "actor": "root", "role": "admin"}]}), cfg)
        print("CHILD_STATUS", out["result"]["status"])
        print("CHILD_CALLS", len(ads.calls))

asyncio.run(main())
'''


async def test_resume_in_real_child_process(sessionmaker_, tmp_path):
    """父进程挂起审批 → 关掉连接 → 子进程（新进程）恢复并执行。"""
    biz_db = tmp_path / "biz.db"
    cp_db = str(tmp_path / "cp.sqlite")

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from hub.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{biz_db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    await seed_ads(sm, "S1")

    platform = InMemoryPlatform({("S1", "kw1"): 1.0})
    ads = LocalAdsAdapter(platform)
    deps = OrchestratorDeps(
        registry=default_registry(AdvertisingAgent(AdvertisingDeps(
            sessionmaker=sm, ads=ads, llm_factory=make_llm, today=lambda: DAY))),
        sessionmaker=sm, ads=ads, model_mode="fake")

    async with open_sqlite_checkpointer(cp_db) as saver:
        graph = build_graph(deps, checkpointer=saver)
        cfg = {"configurable": {"thread_id": "restart-t"}}
        from hub.orchestration.state import initial_state
        await graph.ainvoke(initial_state(run_id="r", thread_id="restart-t", shop_id="S1",
                                          message="广告花费太高，关键词出价要降"), cfg)
        snap = await graph.aget_state(cfg)
        sid = snap.values["pending_approvals"][0]["suggestion_id"]

    # 父进程的 sqlite 连接已关闭，下面用全新进程恢复
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": f"sqlite+aiosqlite:///{biz_db}",
        "GRAPH_CHECKPOINT_DB": cp_db,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "PYTHONIOENCODING": "utf-8",
    })
    script = tmp_path / "child_resume.py"
    script.write_text(CHILD, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), "restart-t", sid],
                          cwd=str(Path(__file__).resolve().parents[2]),
                          env=env, capture_output=True, text=True, timeout=120)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "CHILD_SHOP S1" in out, out
    assert "CHILD_PENDING 1" in out, out
    assert "CHILD_STATUS completed" in out, out
    assert "CHILD_CALLS 1" in out, out

    await engine.dispose()
    # 业务库里该建议已经是 EXECUTED（子进程真的执行了，不是假装）
    from sqlalchemy.ext.asyncio import create_async_engine as cae2
    eng2 = cae2(f"sqlite+aiosqlite:///{biz_db}")
    sm2 = async_sessionmaker(eng2, expire_on_commit=False)
    async with sm2() as s:
        st = (await s.execute(select(Suggestion.status).where(Suggestion.id == sid))).scalar_one()
    assert st == "EXECUTED"
    await eng2.dispose()
