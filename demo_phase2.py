r"""阶段二演示：模型提建议 -> 后端证据校验 -> 规则校验 -> 风险分流 -> 审批 -> 执行 -> 回滚 -> 审计。

运行（在 amazon-agent-hub 目录下）：
    .\.venv\Scripts\python.exe demo_phase2.py

全程离线：模型是 FakeLLM（按脚本回答），广告平台是内存版 InMemoryPlatform。
演示的目的不是“诊断准不准”，而是证明模型的建议无法绕过任何一道后端保护。
"""
import asyncio
import sys
from pathlib import Path

# Windows 控制台默认不是 UTF-8，显式设成 UTF-8 避免中文输出乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from hub.agent.agent import run_diagnosis
from hub.agent.approval import (DEFAULT_RULES, approve, execute, rollback,
                                submit_suggestion)
from hub.agent.llm import FakeLLM, LLMResponse
from hub.agent.pipeline import diagnose_and_submit
from hub.agent.platform import InMemoryPlatform
from hub.agent.tools import ToolContext, ToolError, dispatch
from hub.models import (AdReport, ApprovalLog, Base, DiagnosisRun, Inventory,
                        Suggestion, ToolCallLog)

DB = Path("demo_phase2.db")
CTX = ToolContext(shop_id="S1", today="2026-01-10")
DAYS = ("2026-01-08", "2026-01-09", "2026-01-10")


def line(title=""):
    print("\n" + "=" * 78)
    if title:
        print(title)
        print("=" * 78)


def tool_call(days=3):
    return LLMResponse(kind="tool_call", tool="get_ad_summary", args={"days": days}, tokens=80)


def final(after, before=1.0, target="kw1", value=52.2, call_id="c1", reason="花费高且无订单"):
    return LLMResponse(kind="final", tokens=120, final={"suggestions": [{
        "action": "adjust_bid", "target": target, "before": before, "after": after,
        "reason": reason,
        "evidence": [{"call_id": call_id, "keyword_id": target,
                      "field": "spend", "value": value}]}]})


def sug(before=1.0, ratio=0.08, target="kw1"):
    return {"action": "adjust_bid", "target": target, "before": before,
            "after": round(before * (1 - ratio), 4), "reason": "演示", "evidence": []}


async def main():
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB) + suffix)
        if p.exists():
            p.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB}", connect_args={"timeout": 30})
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

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
            s.add(Inventory(shop_id="S1", sku="SKU1", quantity=7))
    platform = InMemoryPlatform({("S1", "kw1"): 1.0, ("S1", "kw2"): 0.8, ("S2", "kw1"): 2.0})

    line("阶段二演示：诊断 Agent + 审批链（模型只能提建议，不能直接改线上数据）")
    print("店铺 S1 有 3 天广告数据：kw1 phone case 无订单、kw2 usb cable 有订单；")
    print("平台当前出价：S1/kw1 = 1.00，S1/kw2 = 0.80。模型全程是脚本化的 FakeLLM。")

    # ---------- 1. 工具白名单 / 参数校验 / 店铺隔离 ----------
    line("[1] 工具白名单与参数校验：模型能不能乱调工具、乱传参数？")
    async with sm() as s:
        for name, args, label in [
            ("delete_everything", {}, "调用未登记的工具"),
            ("get_ad_summary", {"days": 3, "shop_id": "S2"}, "自己加 shop_id 想查别人店铺"),
            ("get_ad_summary", {"days": 999}, "参数越界"),
        ]:
            try:
                await dispatch(s, CTX, name, args)
                print(f"    {label}: 居然成功了（不应该）")
            except ToolError as e:
                print(f"    {label}: 被拒绝 -> ToolError({e.code})")
        data = await dispatch(s, CTX, "get_ad_summary", {"days": 3})
    kw1 = next(k for k in data["keywords"] if k["keyword_id"] == "kw1")
    print(f"    正常查询: kw1 spend={kw1['spend']} orders={kw1['orders']}"
          f"（S2 同 keyword_id 的 999.0 不可见，店铺由后端注入）")

    # ---------- 2. 死循环 / 成本上限 ----------
    line("[2] 防死循环、防成本失控：模型一直要工具会怎样？")
    class Looping:
        async def next(self, messages):
            return LLMResponse(kind="tool_call", tool="get_inventory",
                               args={"sku": "SKU1"}, tokens=80)
    r = await run_diagnosis(sm, Looping(), CTX, "一直查库存", max_steps=10, max_tool_calls=4)
    print(f"    run#{r.run_id} status={r.status} steps={r.steps} tokens={r.tokens}"
          f"（工具只执行了 4 次后强制终止）")

    # ---------- 3. 完整链路 ----------
    line("[3] 完整链路：真实证据的建议 -> 高风险 -> 审批 -> 执行 -> 回滚 -> 审计")
    out = await diagnose_and_submit(sm, platform, FakeLLM([tool_call(), final(0.70)]), CTX,
                                    "kw1 花费高且没有订单，诊断一下")
    run, o = out["run"], out["outcomes"][0]
    print(f"    诊断 run#{run.run_id}: status={run.status} steps={run.steps} tokens={run.tokens}")
    print(f"    建议: kw1 出价 1.00 -> 0.70（调幅 30%，证据 spend=52.2 来自本次运行 c1）")
    print(f"    证据校验: 通过（dropped_no_evidence={out['dropped_no_evidence']}）")
    print(f"    规则校验: 通过（min_bid/max_change_ratio/daily_limit 均未触发）")
    print(f"    风险分流: risk={o.risk} -> status={o.status}")
    print(f"    运营 bob 审批: {await approve(sm, o.suggestion_id, 'bob', 'operator')}"
          f"（高风险只有管理员能批）")
    print(f"    管理员 alice 审批: {await approve(sm, o.suggestion_id, 'alice', 'admin')}")
    print(f"    执行: {await execute(sm, o.suggestion_id, platform)}，平台出价 -> "
          f"{platform.bids[('S1', 'kw1')]:.2f}")
    print(f"    回滚: {await rollback(sm, o.suggestion_id, platform)}，平台出价恢复 -> "
          f"{platform.bids[('S1', 'kw1')]:.2f}")
    async with sm() as s:
        logs = (await s.execute(select(ApprovalLog).where(
            ApprovalLog.suggestion_id == o.suggestion_id).order_by(ApprovalLog.id))).scalars().all()
    print("    审计日志（每一步都有操作人/结果/原因）：")
    for log in logs:
        detail = f" ({log.detail})" if log.detail else ""
        print(f"      - {log.step:<13} actor={log.actor:<8} result={log.result:<9}{detail}")

    # ---------- 4. 编造证据 ----------
    line("[4] 编造证据：模型引用不存在的数值/调用编号")
    out = await diagnose_and_submit(sm, platform, FakeLLM([tool_call(), final(0.70, value=999.0)]),
                                    CTX, "编造 spend")
    print(f"    引用不存在的数值 999.0 -> dropped_no_evidence={out['dropped_no_evidence']}，"
          f"进入审批的建议数={len(out['outcomes'])}")
    out = await diagnose_and_submit(sm, platform,
                                    FakeLLM([tool_call(), final(0.70, call_id="c9")]),
                                    CTX, "编造调用编号")
    print(f"    引用不存在的调用编号 c9 -> dropped_no_evidence={out['dropped_no_evidence']}，"
          f"进入审批的建议数={len(out['outcomes'])}")

    # ---------- 5. 规则拦截 ----------
    line("[5] 规则校验：证据是真的，但出价离谱（0.01）")
    before_calls = len(platform.calls)
    out = await diagnose_and_submit(sm, platform, FakeLLM([tool_call(), final(0.01)]), CTX,
                                    "把出价打到 0.01")
    o = out["outcomes"][0]
    print(f"    status={o.status} risk={o.risk} violations={o.violations}")
    print(f"    平台 set_bid 调用次数变化: {len(platform.calls) - before_calls}（被规则拦下，一次都没调）")

    # ---------- 6. 并发执行只生效一次 ----------
    line("[6] 并发执行：20 个重复点击/重复调度同时到达")
    low = await submit_suggestion(sm, platform, "S1", 1, CTX.today, sug(ratio=0.08))
    print(f"    低风险建议: risk={low.risk} status={low.status}（自动通过，无需人工）")
    before_calls = len(platform.calls)
    res = await asyncio.gather(*[execute(sm, low.suggestion_id, platform) for _ in range(20)])
    print(f"    20 个并发 execute 结果: executed={res.count('executed')} "
          f"skipped={res.count('skipped')}，平台实际调用 {len(platform.calls) - before_calls} 次")
    print(f"    平台出价 -> {platform.bids[('S1', 'kw1')]:.2f}（只改了一次）")

    # ---------- 7. 并发审批只成功一次 ----------
    line("[7] 并发审批：20 个人同时点“批准”")
    mid = await submit_suggestion(sm, platform, "S1", 1, "2026-01-11",
                                  sug(before=0.8, ratio=0.15, target="kw2"))
    print(f"    中风险建议: risk={mid.risk} status={mid.status}")
    res = await asyncio.gather(*[approve(sm, mid.suggestion_id, f"u{i}", "operator")
                                 for i in range(20)])
    print(f"    20 个并发 approve 结果: approved={res.count('approved')} "
          f"conflict={res.count('conflict')}")

    # ---------- 8. 审批期间被改价 ----------
    line("[8] 审批期间别人改了价：执行前再核对一次旧值")
    platform.bids[("S1", "kw1")] = 1.0     # 上一步低风险建议执行后是 0.92，这里复原成 1.00
    p = await submit_suggestion(sm, platform, "S1", 1, "2026-01-12",
                                sug(before=1.0, ratio=0.15, target="kw1"))
    await approve(sm, p.suggestion_id, "alice", "admin")
    platform.bids[("S1", "kw1")] = 1.2          # 模拟审批期间线上出价被别人改了
    print(f"    审批通过后线上出价被改成 1.20 -> execute: "
          f"{await execute(sm, p.suggestion_id, platform)}")
    print(f"    平台出价保持 {platform.bids[('S1', 'kw1')]:.2f}，没有被旧建议覆盖")

    # ---------- 汇总 ----------
    line("[汇总] 本次演示落库的数据")
    async with sm() as s:
        n_run = len((await s.execute(select(DiagnosisRun))).scalars().all())
        n_tool = len((await s.execute(select(ToolCallLog))).scalars().all())
        n_sug = len((await s.execute(select(Suggestion))).scalars().all())
        n_log = len((await s.execute(select(ApprovalLog))).scalars().all())
        statuses = (await s.execute(select(Suggestion.risk, Suggestion.status))).all()
    print(f"    diagnosis_run={n_run}  tool_call_log={n_tool}  suggestion={n_sug}  approval_log={n_log}")
    print(f"    建议风险/状态分布: {sorted(statuses)}")
    print(f"\n    演示数据库: {DB.resolve()}（可用 sqlite3 打开查看表数据）")
    print("    规则参数:", DEFAULT_RULES)
    print("    结论: 模型只能提出建议；证据、规则、审批、幂等、执行前核对、回滚快照和审计"
          "全部由后端把关，任何一环不满足都改不了线上数据。")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
