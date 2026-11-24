import asyncio

import pytest
from sqlalchemy import select

from hub.agent.agent import run_diagnosis
from hub.agent.approval import approve, execute, reject, rollback, submit_suggestion
from hub.agent.llm import FakeLLM, LLMResponse
from hub.agent.pipeline import diagnose_and_submit
from hub.agent.platform import InMemoryPlatform
from hub.agent.tools import ToolContext, ToolError, dispatch
from hub.models import AdReport, ApprovalLog, Inventory, Suggestion, ToolCallLog

CTX = ToolContext(shop_id="S1", today="2026-01-10")
DAYS = ("2026-01-08", "2026-01-09", "2026-01-10")


@pytest.fixture
async def seeded(sessionmaker_):
    async with sessionmaker_() as s:
        async with s.begin():
            for d in DAYS:
                s.add_all([
                    AdReport(shop_id="S1", day=d, keyword_id="kw1", keyword="phone case",
                             spend=17.4, clicks=27, orders=0),
                    AdReport(shop_id="S1", day=d, keyword_id="kw2", keyword="usb cable",
                             spend=3.0, clicks=10, orders=5),
                    # 另一个店铺使用了相同的 keyword_id，用来检验隔离
                    AdReport(shop_id="S2", day=d, keyword_id="kw1", keyword="other shop",
                             spend=333.0, clicks=500, orders=1),
                ])
            s.add(Inventory(shop_id="S1", sku="SKU1", quantity=7))
    return sessionmaker_


@pytest.fixture
def platform():
    return InMemoryPlatform({("S1", "kw1"): 1.0, ("S1", "kw2"): 0.8, ("S2", "kw1"): 2.0})


def call_tool(tool="get_ad_summary", args=None):
    return LLMResponse(kind="tool_call", tool=tool, args=args if args is not None else {"days": 3}, tokens=80)


def final(after=0.7, before=1.0, target="kw1", value=52.2, call_id="c1"):
    return LLMResponse(kind="final", tokens=120, final={"suggestions": [{
        "action": "adjust_bid", "target": target, "before": before, "after": after,
        "reason": "花费高且无订单",
        "evidence": [{"call_id": call_id, "keyword_id": target, "field": "spend", "value": value}]}]})


def sug(before=1.0, ratio=0.08, target="kw1"):
    return {"action": "adjust_bid", "target": target, "before": before,
            "after": round(before * (1 - ratio), 4), "reason": "t", "evidence": []}


async def status_of(sm, sid):
    async with sm() as s:
        return (await s.execute(select(Suggestion.status).where(Suggestion.id == sid))).scalar_one()


# ---------- 工具层 ----------

async def test_whitelist_and_args_validation(seeded):
    async with seeded() as s:
        with pytest.raises(ToolError) as e:
            await dispatch(s, CTX, "delete_everything", {})
        assert e.value.code == "tool_not_allowed"
        with pytest.raises(ToolError) as e:           # 模型试图指定别的店铺
            await dispatch(s, CTX, "get_ad_summary", {"days": 3, "shop_id": "S2"})
        assert e.value.code == "bad_args"
        with pytest.raises(ToolError):                # 参数越界
            await dispatch(s, CTX, "get_ad_summary", {"days": 999})


async def test_shop_isolation(seeded):
    async with seeded() as s:
        r = await dispatch(s, CTX, "get_ad_summary", {"days": 3})
    kw1 = next(k for k in r["keywords"] if k["keyword_id"] == "kw1")
    assert kw1["spend"] == 52.2 and kw1["orders"] == 0     # 看到的是 S1，不是 S2 的 999.0
    assert all(k["spend"] < 100 for k in r["keywords"])


# ---------- Agent 循环 ----------

async def test_loop_limits(seeded):
    class Looping:
        async def next(self, messages):
            return call_tool("get_inventory", {"sku": "SKU1"})
    r = await run_diagnosis(seeded, Looping(), CTX, "t", max_steps=10, max_tool_calls=4)
    assert r.status == "aborted_too_many_calls"
    async with seeded() as s:
        n = len((await s.execute(select(ToolCallLog))).scalars().all())
    assert n == 4

    r = await run_diagnosis(seeded, Looping(), CTX, "t", max_steps=3, max_tool_calls=10)
    assert r.status == "aborted_max_steps" and r.steps == 3


async def test_forbidden_tool_is_logged_not_executed(seeded):
    llm = FakeLLM([call_tool("delete_everything", {}), LLMResponse(kind="final", final={}, tokens=10)])
    r = await run_diagnosis(seeded, llm, CTX, "t")
    assert r.status == "completed"
    async with seeded() as s:
        row = (await s.execute(select(ToolCallLog))).scalars().one()
    assert row.ok == 0 and row.result == {"error": "tool_not_allowed"}


async def test_token_cost_recorded(seeded):
    r = await run_diagnosis(seeded, FakeLLM([call_tool(), final()]), CTX, "t")
    assert r.tokens == 200 and r.steps == 2


# ---------- 证据校验 ----------

async def test_valid_evidence_creates_suggestion(seeded, platform):
    out = await diagnose_and_submit(seeded, platform, FakeLLM([call_tool(), final()]), CTX, "广告烧钱")
    assert out["dropped_no_evidence"] == 0
    assert out["outcomes"][0].status == "PENDING_APPROVAL" and out["outcomes"][0].risk == "high"


async def test_fabricated_evidence_dropped(seeded, platform):
    out = await diagnose_and_submit(seeded, platform, FakeLLM([call_tool(), final(value=999.0)]), CTX, "t")
    assert out["outcomes"] == [] and out["dropped_no_evidence"] == 1
    out = await diagnose_and_submit(seeded, platform, FakeLLM([call_tool(), final(call_id="c9")]), CTX, "t")
    assert out["outcomes"] == [] and out["dropped_no_evidence"] == 1


async def test_bad_model_output_blocked_by_rules(seeded, platform):
    """证据是真的，但模型给出离谱出价：规则层必须拦住，平台一次都不会被调用。"""
    out = await diagnose_and_submit(seeded, platform, FakeLLM([call_tool(), final(after=0.01)]), CTX, "t")
    o = out["outcomes"][0]
    assert o.status == "REJECTED_BY_RULE"
    assert {"below_min_bid", "change_too_large"} <= set(o.violations)
    assert platform.calls == []


# ---------- 规则与分流 ----------

async def test_rules(seeded, platform):
    platform.bids[("S1", "kw1")] = 1.5                                  # 与建议里的 before 不一致
    o = await submit_suggestion(seeded, platform, "S1", 1, "2026-01-10", sug(before=1.0))
    assert o.status == "REJECTED_BY_RULE" and "stale_before_value" in o.violations
    platform.bids[("S1", "kw1")] = 1.0
    o1 = await submit_suggestion(seeded, platform, "S1", 1, "2026-01-10", sug())
    o2 = await submit_suggestion(seeded, platform, "S1", 1, "2026-01-10", sug())
    o3 = await submit_suggestion(seeded, platform, "S1", 1, "2026-01-10", sug())
    assert (o1.status, o2.status) == ("APPROVED", "APPROVED")
    assert o3.status == "REJECTED_BY_RULE" and "daily_limit_reached" in o3.violations


async def test_risk_routing_and_roles(seeded, platform):
    low = await submit_suggestion(seeded, platform, "S1", 1, "2026-01-10", sug(ratio=0.08, target="kw1"))
    mid = await submit_suggestion(seeded, platform, "S1", 1, "2026-01-10", sug(before=0.8, ratio=0.15, target="kw2"))
    high = await submit_suggestion(seeded, platform, "S1", 1, "2026-01-11", sug(ratio=0.25))
    assert (low.risk, low.status) == ("low", "APPROVED")
    assert (mid.risk, mid.status) == ("medium", "PENDING_APPROVAL")
    assert (high.risk, high.status) == ("high", "PENDING_APPROVAL")
    assert await approve(seeded, high.suggestion_id, "bob", "operator") == "forbidden"
    assert await status_of(seeded, high.suggestion_id) == "PENDING_APPROVAL"
    assert await approve(seeded, high.suggestion_id, "alice", "admin") == "approved"
    assert await approve(seeded, mid.suggestion_id, "bob", "operator") == "approved"
    assert await reject(seeded, mid.suggestion_id, "bob") == "conflict"   # 已批准，不能再拒绝


# ---------- 并发、失败、回滚 ----------

async def test_concurrent_approve_only_one_wins(seeded, platform):
    o = await submit_suggestion(seeded, platform, "S1", 1, "2026-01-10", sug(ratio=0.15))
    res = await asyncio.gather(*[approve(seeded, o.suggestion_id, f"u{i}", "operator") for i in range(20)])
    assert res.count("approved") == 1 and res.count("conflict") == 19


async def test_concurrent_execute_applies_once(seeded, platform):
    o = await submit_suggestion(seeded, platform, "S1", 1, "2026-01-10", sug())
    res = await asyncio.gather(*[execute(seeded, o.suggestion_id, platform) for _ in range(20)])
    assert res.count("executed") == 1 and res.count("skipped") == 19
    assert len(platform.calls) == 1 and platform.bids[("S1", "kw1")] == 0.92


async def test_execute_failure_is_final_and_safe(seeded, platform):
    o = await submit_suggestion(seeded, platform, "S1", 1, "2026-01-10", sug())
    platform.fail_next = 1
    assert await execute(seeded, o.suggestion_id, platform) == "failed"
    assert await status_of(seeded, o.suggestion_id) == "FAILED"
    assert await execute(seeded, o.suggestion_id, platform) == "skipped"     # 失败后不会被重复执行
    assert platform.bids[("S1", "kw1")] == 1.0


async def test_stale_at_execute_time(seeded, platform):
    o = await submit_suggestion(seeded, platform, "S1", 1, "2026-01-10", sug())
    platform.bids[("S1", "kw1")] = 1.2          # 审批期间，别人已经改了出价
    assert await execute(seeded, o.suggestion_id, platform) == "failed"
    assert platform.bids[("S1", "kw1")] == 1.2 and platform.calls == []


async def test_rollback_restores_before_value(seeded, platform):
    o = await submit_suggestion(seeded, platform, "S1", 1, "2026-01-10", sug())
    await execute(seeded, o.suggestion_id, platform)
    assert platform.bids[("S1", "kw1")] == 0.92
    assert await rollback(seeded, o.suggestion_id, platform) == "rolled_back"
    assert platform.bids[("S1", "kw1")] == 1.0
    assert await rollback(seeded, o.suggestion_id, platform) == "skipped"    # 不能重复回滚


async def test_audit_trail(seeded, platform):
    o = await submit_suggestion(seeded, platform, "S1", 1, "2026-01-10", sug(ratio=0.25))
    await approve(seeded, o.suggestion_id, "alice", "admin")
    await execute(seeded, o.suggestion_id, platform)
    async with seeded() as s:
        logs = (await s.execute(select(ApprovalLog).where(
            ApprovalLog.suggestion_id == o.suggestion_id).order_by(ApprovalLog.id))).scalars().all()
    assert [(l.step, l.actor, l.result) for l in logs] == [
        ("rule_check", "system", "pass"), ("approve", "alice", "ok"),
        ("execute_start", "system", "ok"), ("execute_done", "system", "ok")]
