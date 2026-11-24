from .agent import evidence_ok, run_diagnosis
from .approval import submit_suggestion
from .tools import ToolContext


async def diagnose_and_submit(sm, platform, llm, ctx: ToolContext, scenario: str, rules=None) -> dict:
    """诊断 -> 证据校验 -> 规则校验与分流。模型的建议不会直接触达线上。"""
    result = await run_diagnosis(sm, llm, ctx, scenario)
    outcomes, dropped = [], 0
    for sug in result.final.get("suggestions", []):
        if not await evidence_ok(sm, result.run_id, sug):
            dropped += 1                  # 没有真实证据的建议直接丢弃，不进审批
            continue
        outcomes.append(await submit_suggestion(
            sm, platform, ctx.shop_id, result.run_id, ctx.today, sug, rules))
    return {"run": result, "outcomes": outcomes, "dropped_no_evidence": dropped}
