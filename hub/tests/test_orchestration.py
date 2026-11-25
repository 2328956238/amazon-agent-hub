"""S3 编排层测试：意图、注册表、图路由、未实现拒绝、异常、审批中断与恢复、跨重启持久化。"""

import pytest
from sqlalchemy import select

from hub.adapters.local_ads import LocalAdsAdapter
from hub.agent.llm import FakeLLM, LLMResponse
from hub.agent.platform import InMemoryPlatform
from hub.models import AdReport, Suggestion
from hub.orchestration.agents.advertising import AdvertisingAgent, AdvertisingDeps
from hub.orchestration.checkpointer import open_sqlite_checkpointer
from hub.orchestration.graph import build_graph
from hub.orchestration.intent import classify, classify_by_rules
from hub.orchestration.nodes import OrchestratorDeps
from hub.orchestration.registry import (
    AgentRegistry,
    UnimplementedAgent,
    default_registry,
)
from hub.orchestration.state import Intent, RunStatus, initial_state

CTX_DAY = "2026-01-10"
DAYS = ("2026-01-08", "2026-01-09", "2026-01-10")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
async def seed_ads(sm):
    async with sm() as s:
        async with s.begin():
            for d in DAYS:
                s.add_all([
                    AdReport(shop_id="S1", day=d, keyword_id="kw1", keyword="phone case",
                             spend=17.4, clicks=27, orders=0),
                    AdReport(shop_id="S1", day=d, keyword_id="kw2", keyword="usb cable",
                             spend=3.0, clicks=10, orders=5),
                    AdReport(shop_id="S2", day=d, keyword_id="kw1", keyword="other shop",
                             spend=333.0, clicks=500, orders=1),
                ])


def make_llm():
    """和 test_agent.py 一样的脚本：先查广告，再给出高调幅建议（high 风险，需 admin 审批）。"""
    return FakeLLM([
        LLMResponse(kind="tool_call", tool="get_ad_summary", args={"days": 3}, tokens=80),
        LLMResponse(kind="final", tokens=120, final={"suggestions": [{
            "action": "adjust_bid", "target": "kw1", "before": 1.0, "after": 0.7,
            "reason": "花费高且无订单",
            "evidence": [{"call_id": "c1", "keyword_id": "kw1", "field": "spend", "value": 52.2}],
        }]}),
    ])


def make_deps(sm, platform=None, llm_factory=make_llm, intent_llm=None, registry=None):
    ads = LocalAdsAdapter(platform or InMemoryPlatform({("S1", "kw1"): 1.0, ("S1", "kw2"): 0.8}))
    agent = AdvertisingAgent(AdvertisingDeps(
        sessionmaker=sm, ads=ads, llm_factory=llm_factory, today=lambda: CTX_DAY))
    reg = registry or default_registry(agent)
    return OrchestratorDeps(registry=reg, sessionmaker=sm, ads=ads, intent_llm=intent_llm), ads


def state(msg="广告花费太高，帮我看下出价", shop="S1", thread="t1", run="r1"):
    return initial_state(run_id=run, thread_id=thread, shop_id=shop, message=msg)


CFG = {"configurable": {"thread_id": "t1"}}


# ---------------------------------------------------------------------------
# 意图识别
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("msg,expected", [
    ("广告 ACOS 太高了", Intent.ADVERTISING),
    ("关键词出价要降一点", Intent.ADVERTISING),
    ("这个 SKU 库存快没了，要补货", Intent.INVENTORY),
    ("价格要不要降 2 美金", Intent.PRICING),
    ("帮我把 listing 标题优化一下", Intent.PRODUCT),
])
def test_rule_intent(msg, expected):
    intent, score = classify_by_rules(msg)
    assert intent is expected and score > 0


def test_unknown_intent_when_no_rules_hit():
    intent, score = classify_by_rules("今天天气怎么样")
    assert intent is Intent.UNKNOWN and score == 0


async def test_llm_fallback_only_when_rules_miss():
    async def llm(msg):
        return "inventory"

    intent, source = await classify("这个货好像不太够了", llm)
    assert (intent, source) == (Intent.INVENTORY, "llm")

    intent, source = await classify("广告花费太高", llm)   # 规则命中就不调模型
    assert (intent, source) == (Intent.ADVERTISING, "rule")


async def test_llm_failure_degrades_to_unknown():
    async def boom(msg):
        raise RuntimeError("model down")

    intent, source = await classify("啊这", boom)
    assert intent is Intent.UNKNOWN and source == "fallback"


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
def test_default_registry_marks_unimplemented():
    reg = default_registry(AdvertisingAgent(AdvertisingDeps(None, None, make_llm, lambda: CTX_DAY)))
    desc = {d["name"]: d for d in reg.describe()}
    assert desc["advertising"]["implemented"] is True
    for name in ("product", "inventory", "pricing"):
        assert desc[name]["implemented"] is False
        assert desc[name]["implemented"] is False and desc[name]["intents"]


def test_registry_rejects_duplicates():
    reg = AgentRegistry()
    a = UnimplementedAgent("a", (Intent.PRODUCT,))
    reg.register(a)
    with pytest.raises(ValueError):
        reg.register(a)
    with pytest.raises(ValueError):     # 同一个意图被第二个 Agent 抢注
        reg.register(UnimplementedAgent("b", (Intent.PRODUCT,)))


# ---------------------------------------------------------------------------
# 图：正常路径 / 未实现 / 无法识别
# ---------------------------------------------------------------------------
async def test_graph_unimplemented_agent_is_explicit(sessionmaker_):
    deps, _ = make_deps(sessionmaker_)
    app = build_graph(deps, allow_memory_fallback=True)
    out = await app.ainvoke(state("库存快没了，要补货"), CFG)
    summary = out["result"]
    assert summary["status"] == RunStatus.NOT_IMPLEMENTED.value
    assert summary["agent"] == "inventory"
    assert summary["agent_implemented"] is False
    assert "尚未实现" in summary["payload"]["message"]


async def test_graph_unknown_intent_fails_without_side_effects(sessionmaker_):
    deps, ads = make_deps(sessionmaker_)
    app = build_graph(deps, allow_memory_fallback=True)
    out = await app.ainvoke(state("今天天气怎么样"), CFG)
    summary = out["result"]
    assert summary["status"] == RunStatus.FAILED.value
    assert summary["errors"] and "没有 Agent 能处理" in summary["errors"][0]
    assert ads.calls == []                       # 没有写操作


async def test_graph_exception_becomes_failed(sessionmaker_):
    class BrokenAgent:
        name, intents, implemented = "broken", (Intent.ADVERTISING,), True

        async def run(self, state):
            raise RuntimeError("downstream exploded")

    reg = AgentRegistry()
    reg.register(BrokenAgent())
    deps, ads = make_deps(sessionmaker_, registry=reg)
    out = await build_graph(deps, allow_memory_fallback=True).ainvoke(state(), CFG)
    assert out["result"]["status"] == RunStatus.FAILED.value
    assert "downstream exploded" in out["result"]["errors"][0]
    assert ads.calls == []


# ---------------------------------------------------------------------------
# 图：广告诊断 → 待审批（写操作仍未发生）
# ---------------------------------------------------------------------------
async def test_advertising_flow_stops_at_approval(sessionmaker_):
    await seed_ads(sessionmaker_)
    deps, ads = make_deps(sessionmaker_)
    app = build_graph(deps, allow_memory_fallback=True)
    out = await app.ainvoke(state(), CFG)

    assert "__interrupt__" in out                     # 停在审批闸门
    snap = await app.aget_state(CFG)
    assert snap.next == ("approval_gate",)
    summary = snap.values["result"] if snap.values.get("result") else {}
    pending = snap.values["pending_approvals"]
    assert pending and pending[0]["status"] == "PENDING_APPROVAL"

    # 关键安全断言：图跑到这里，平台一次都没被写过
    assert ads.calls == []


# ---------------------------------------------------------------------------
# 图：审批恢复（通过 / 拒绝）
# ---------------------------------------------------------------------------
async def test_resume_approve_executes_through_existing_approval_chain(sessionmaker_):
    from langgraph.types import Command

    await seed_ads(sessionmaker_)
    deps, ads = make_deps(sessionmaker_)
    app = build_graph(deps, allow_memory_fallback=True)
    await app.ainvoke(state(), CFG)

    pending = (await app.aget_state(CFG)).values["pending_approvals"]
    sid = pending[0]["suggestion_id"]

    out = await app.ainvoke(Command(resume={"items": [{
        "suggestion_id": sid, "decision": "approve", "actor": "alice", "role": "admin",
    }]}), CFG)

    assert out["result"]["status"] == RunStatus.COMPLETED.value
    assert out["result"]["approval_results"][0]["result"] == "executed"
    assert len(ads.calls) == 1                          # 只写了一次
    assert ads.bids[("S1", "kw1")] == 0.7

    async with sessionmaker_() as s:
        st = (await s.execute(select(Suggestion.status).where(Suggestion.id == sid))).scalar_one()
    assert st == "EXECUTED"


async def test_resume_reject_does_not_write(sessionmaker_):
    from langgraph.types import Command

    await seed_ads(sessionmaker_)
    deps, ads = make_deps(sessionmaker_)
    app = build_graph(deps, allow_memory_fallback=True)
    await app.ainvoke(state(), CFG)

    pending = (await app.aget_state(CFG)).values["pending_approvals"]
    sid = pending[0]["suggestion_id"]
    out = await app.ainvoke(Command(resume={"items": [{
        "suggestion_id": sid, "decision": "reject", "actor": "bob", "role": "operator",
    }]}), CFG)

    assert out["result"]["status"] == RunStatus.COMPLETED.value
    assert ads.calls == []                              # 拒绝就不执行
    async with sessionmaker_() as s:
        st = (await s.execute(select(Suggestion.status).where(Suggestion.id == sid))).scalar_one()
    assert st == "REJECTED"


async def test_resume_without_decision_keeps_waiting(sessionmaker_):
    from langgraph.types import Command

    await seed_ads(sessionmaker_)
    deps, ads = make_deps(sessionmaker_)
    app = build_graph(deps, allow_memory_fallback=True)
    await app.ainvoke(state(), CFG)
    out = await app.ainvoke(Command(resume={"items": []}), CFG)
    assert out["result"]["status"] == RunStatus.AWAITING_APPROVAL.value
    assert ads.calls == []


# ---------------------------------------------------------------------------
# 持久化：跨连接（模拟进程重启）恢复
# ---------------------------------------------------------------------------
async def test_checkpoint_persists_across_restart(sessionmaker_, tmp_path):
    from langgraph.types import Command

    db = str(tmp_path / "cp.sqlite")
    await seed_ads(sessionmaker_)
    deps, ads = make_deps(sessionmaker_)

    async with open_sqlite_checkpointer(db) as saver:
        app = build_graph(deps, checkpointer=saver)
        await app.ainvoke(state(thread="persist"), {"configurable": {"thread_id": "persist"}})
        sid = (await app.aget_state({"configurable": {"thread_id": "persist"}})) \
            .values["pending_approvals"][0]["suggestion_id"]

    # 关掉连接（进程重启），重新打开同一个文件
    async with open_sqlite_checkpointer(db) as saver2:
        app2 = build_graph(deps, checkpointer=saver2)
        cfg = {"configurable": {"thread_id": "persist"}}
        snap = await app2.aget_state(cfg)
        assert snap.values["shop_id"] == "S1"
        assert snap.values["pending_approvals"][0]["suggestion_id"] == sid

        out = await app2.ainvoke(Command(resume={"items": [{
            "suggestion_id": sid, "decision": "approve", "actor": "root", "role": "admin",
        }]}), cfg)
        assert out["result"]["status"] == RunStatus.COMPLETED.value
        assert ads.bids[("S1", "kw1")] == 0.7
