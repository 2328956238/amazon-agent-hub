"""任务三：执行失败 / 结果未知 / 回滚安全 的测试。

区分两种失败语义：
- failed：确定失败（如本地校验失败、平台明确报错）→ 标记 FAILED，不会自动回滚
- unknown：结果未知（超时/网络中断）→ 平台可能已生效 → 标记 UNKNOWN，**禁止自动回滚**
"""

import httpx
from sqlalchemy import select

from hub.agent.approval import execute, rollback, submit_suggestion
from hub.agent.llm import FakeLLM, LLMResponse
from hub.agent.platform import InMemoryPlatform
from hub.models import AdReport, ApprovalLog, Suggestion
from hub.orchestration.agents.advertising import AdvertisingAgent, AdvertisingDeps
from hub.orchestration.graph import build_graph
from hub.orchestration.nodes import OrchestratorDeps
from hub.orchestration.registry import default_registry

DAY = "2026-01-10"
DAYS = ("2026-01-08", "2026-01-09", "2026-01-10")


def sug(before=1.0, ratio=0.08, target="kw1"):
    return {"action": "adjust_bid", "target": target, "before": before,
            "after": round(before * (1 - ratio), 4), "reason": "t", "evidence": []}


async def status_of(sm, sid):
    async with sm() as s:
        return (await s.execute(select(Suggestion.status).where(Suggestion.id == sid))).scalar_one()


class TimeoutOnSet:
    """get_bid 正常，set_bid 抛超时（模拟请求已发出但没等到响应）。"""

    from hub.adapters.base import AdPlatformMode as _Mode
    mode = _Mode.LOCAL

    def __init__(self, bids):
        self.inner = InMemoryPlatform(bids)
        self.write_attempts = []

    async def get_bid(self, shop_id, keyword_id):
        return await self.inner.get_bid(shop_id, keyword_id)

    async def set_bid(self, shop_id, keyword_id, value, idempotency_key):
        self.write_attempts.append((shop_id, keyword_id, value))
        raise TimeoutError("read timeout after send")


class NetworkErrorOnSet(TimeoutOnSet):
    async def set_bid(self, shop_id, keyword_id, value, idempotency_key):
        self.write_attempts.append((shop_id, keyword_id, value))
        raise httpx.ConnectError("connection reset")


class ExplicitErrorOnSet(TimeoutOnSet):
    async def set_bid(self, shop_id, keyword_id, value, idempotency_key):
        self.write_attempts.append((shop_id, keyword_id, value))
        raise RuntimeError("platform returned 400 invalid bid")


# ---------------------------------------------------------------------------
# 结果未知 vs 确定失败
# ---------------------------------------------------------------------------
async def test_timeout_is_unknown_not_failed(sessionmaker_):
    platform = TimeoutOnSet({("S1", "kw1"): 1.0})
    o = await submit_suggestion(sessionmaker_, platform, "S1", 1, DAY, sug())
    assert await execute(sessionmaker_, o.suggestion_id, platform) == "unknown"
    assert await status_of(sessionmaker_, o.suggestion_id) == "UNKNOWN"
    assert await execute(sessionmaker_, o.suggestion_id, platform) == "skipped"   # 不会重试
    assert platform.inner.bids[("S1", "kw1")] == 1.0                            # 平台未变


async def test_network_error_is_unknown(sessionmaker_):
    platform = NetworkErrorOnSet({("S1", "kw1"): 1.0})
    o = await submit_suggestion(sessionmaker_, platform, "S1", 1, DAY, sug())
    assert await execute(sessionmaker_, o.suggestion_id, platform) == "unknown"
    assert await status_of(sessionmaker_, o.suggestion_id) == "UNKNOWN"


async def test_explicit_platform_error_is_failed(sessionmaker_):
    platform = ExplicitErrorOnSet({("S1", "kw1"): 1.0})
    o = await submit_suggestion(sessionmaker_, platform, "S1", 1, DAY, sug())
    assert await execute(sessionmaker_, o.suggestion_id, platform) == "failed"
    assert await status_of(sessionmaker_, o.suggestion_id) == "FAILED"


async def test_unknown_outcome_never_auto_rolls_back(sessionmaker_):
    """结果未知时 rollback 必须拒绝（状态不是 EXECUTED），不能盲目补偿。"""
    platform = TimeoutOnSet({("S1", "kw1"): 1.0})
    o = await submit_suggestion(sessionmaker_, platform, "S1", 1, DAY, sug())
    await execute(sessionmaker_, o.suggestion_id, platform)
    assert await rollback(sessionmaker_, o.suggestion_id, platform) == "skipped"
    assert len(platform.write_attempts) == 1          # rollback 没有再写一次


# ---------------------------------------------------------------------------
# 回滚安全：不覆盖别人后来的修改
# ---------------------------------------------------------------------------
async def test_rollback_refuses_when_platform_value_changed(sessionmaker_):
    platform = InMemoryPlatform({("S1", "kw1"): 1.0})
    o = await submit_suggestion(sessionmaker_, platform, "S1", 1, DAY, sug())
    assert await execute(sessionmaker_, o.suggestion_id, platform) == "executed"
    assert platform.bids[("S1", "kw1")] == 0.92

    platform.bids[("S1", "kw1")] = 0.5                 # 别人后来改了价
    assert await rollback(sessionmaker_, o.suggestion_id, platform) == "conflict"
    assert platform.bids[("S1", "kw1")] == 0.5          # 没被覆盖
    assert await status_of(sessionmaker_, o.suggestion_id) == "EXECUTED"   # 状态没动

    async with sessionmaker_() as s:
        logs = (await s.execute(select(ApprovalLog).where(
            ApprovalLog.suggestion_id == o.suggestion_id))).scalars().all()
    assert any(l.step == "rollback_precheck" and l.result == "conflict" for l in logs)


async def test_rollback_works_when_value_unchanged(sessionmaker_):
    platform = InMemoryPlatform({("S1", "kw1"): 1.0})
    o = await submit_suggestion(sessionmaker_, platform, "S1", 1, DAY, sug())
    await execute(sessionmaker_, o.suggestion_id, platform)
    assert await rollback(sessionmaker_, o.suggestion_id, platform) == "rolled_back"
    assert platform.bids[("S1", "kw1")] == 1.0


async def test_rollback_precheck_fails_when_platform_unreadable(sessionmaker_):
    class NoRead(TimeoutOnSet):
        async def get_bid(self, *a, **k):
            raise RuntimeError("cannot read")

    platform = InMemoryPlatform({("S1", "kw1"): 1.0})
    o = await submit_suggestion(sessionmaker_, platform, "S1", 1, DAY, sug())
    await execute(sessionmaker_, o.suggestion_id, platform)

    broker = NoRead({("S1", "kw1"): 0.92})
    assert await rollback(sessionmaker_, o.suggestion_id, broker) == "precheck_failed"
    assert await status_of(sessionmaker_, o.suggestion_id) == "EXECUTED"


# ---------------------------------------------------------------------------
# 平台幂等键 / 快照可靠性
# ---------------------------------------------------------------------------
async def test_platform_idempotency_key_prevents_double_apply(sessionmaker_):
    platform = InMemoryPlatform({("S1", "kw1"): 1.0})
    o = await submit_suggestion(sessionmaker_, platform, "S1", 1, DAY, sug())
    await execute(sessionmaker_, o.suggestion_id, platform)
    # 直接再用同一个幂等键写一次（模拟重复调度），平台侧不应二次生效
    await platform.set_bid("S1", "kw1", 0.1, idempotency_key=o.suggestion_id)
    assert platform.bids[("S1", "kw1")] == 0.92


async def test_before_value_snapshot_is_used_and_guarded(sessionmaker_):
    platform = InMemoryPlatform({("S1", "kw1"): 1.0})
    o = await submit_suggestion(sessionmaker_, platform, "S1", 1, DAY, sug())
    platform.bids[("S1", "kw1")] = 1.2                 # 审批期间被改
    assert await execute(sessionmaker_, o.suggestion_id, platform) == "failed"
    assert platform.bids[("S1", "kw1")] == 1.2          # 不会覆盖


# ---------------------------------------------------------------------------
# 编排层：unknown 要冒到 run 状态，并且不触发补偿
# ---------------------------------------------------------------------------
async def seed_ads(sm):
    async with sm() as s:
        async with s.begin():
            for d in DAYS:
                s.add(AdReport(shop_id="S1", day=d, keyword_id="kw1", keyword="phone case",
                               spend=17.4, clicks=27, orders=0))


def make_llm():
    return FakeLLM([
        LLMResponse(kind="tool_call", tool="get_ad_summary", args={"days": 3}, tokens=80),
        LLMResponse(kind="final", tokens=120, final={"suggestions": [{
            "action": "adjust_bid", "target": "kw1", "before": 1.0, "after": 0.7,
            "reason": "花费高且无订单",
            "evidence": [{"call_id": "c1", "keyword_id": "kw1", "field": "spend", "value": 52.2}],
        }]}),
    ])


async def test_graph_marks_run_failed_and_requires_manual_check_on_unknown(sessionmaker_):
    from langgraph.types import Command

    await seed_ads(sessionmaker_)
    platform = TimeoutOnSet({("S1", "kw1"): 1.0})
    deps = OrchestratorDeps(
        registry=default_registry(AdvertisingAgent(AdvertisingDeps(
            sessionmaker=sessionmaker_, ads=platform, llm_factory=make_llm, today=lambda: DAY))),
        sessionmaker=sessionmaker_, ads=platform, model_mode="fake")
    graph = build_graph(deps, allow_memory_fallback=True)
    cfg = {"configurable": {"thread_id": "unknown-t"}}
    from hub.orchestration.state import initial_state
    await graph.ainvoke(initial_state(run_id="r", thread_id="unknown-t", shop_id="S1",
                                      message="广告花费太高，关键词出价要降"), cfg)
    sid = (await graph.aget_state(cfg)).values["pending_approvals"][0]["suggestion_id"]

    out = await graph.ainvoke(Command(resume={"items": [{
        "suggestion_id": sid, "decision": "approve", "actor": "alice", "role": "admin"}]}), cfg)

    result = out["result"]
    assert result["status"] == "failed"
    assert any("结果未知" in e and "不会自动回滚" in e for e in result["errors"])
    assert result["approval_results"][0]["result"] == "unknown"
    assert len(platform.write_attempts) == 1           # 只尝试了一次，没有补偿写
    assert await status_of(sessionmaker_, sid) == "UNKNOWN"
