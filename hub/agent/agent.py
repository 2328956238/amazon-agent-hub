from dataclasses import dataclass, field

from sqlalchemy import select, update

from ..models import DiagnosisRun, ToolCallLog
from .tools import ToolContext, ToolError, dispatch

SYSTEM_PROMPT = (
    "你是亚马逊广告诊断助手。只能通过工具获取数据；结论必须引用工具返回的数值作为证据；"
    "没有数据依据时不要给建议。"
)


@dataclass
class RunResult:
    run_id: int
    status: str            # completed / aborted_max_steps / aborted_too_many_calls
    steps: int
    tokens: int
    final: dict = field(default_factory=dict)


async def run_diagnosis(sm, llm, ctx: ToolContext, scenario: str,
                        max_steps: int = 5, max_tool_calls: int = 4) -> RunResult:
    async with sm() as s:
        async with s.begin():
            run = DiagnosisRun(shop_id=ctx.shop_id, scenario=scenario, status="running")
            s.add(run)
            await s.flush()
            run_id = run.id

    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": scenario}]
    tokens, calls, steps = 0, 0, 0
    status, final = "aborted_max_steps", {}

    for _ in range(max_steps):          # 步数上限：防死循环
        steps += 1
        resp = await llm.next(messages)
        tokens += resp.tokens
        if resp.kind == "final":
            status, final = "completed", resp.final
            break
        if calls >= max_tool_calls:      # 工具调用次数上限：防成本失控
            status = "aborted_too_many_calls"
            break
        calls += 1
        call_id = f"c{calls}"
        ok = 1
        async with sm() as s:
            async with s.begin():
                try:
                    result = await dispatch(s, ctx, resp.tool, resp.args)
                except ToolError as e:   # 白名单外、参数不合法：不执行，把错误还给模型
                    result, ok = {"error": e.code}, 0
                s.add(ToolCallLog(run_id=run_id, call_id=call_id, tool=resp.tool,
                                  args=resp.args, result=result, ok=ok))
        messages.append({"role": "tool", "call_id": call_id, "content": result})

    async with sm() as s:
        async with s.begin():
            await s.execute(update(DiagnosisRun).where(DiagnosisRun.id == run_id)
                            .values(status=status, steps=steps, token_cost=tokens))
    return RunResult(run_id, status, steps, tokens, final)


async def evidence_ok(sm, run_id: int, sug: dict) -> bool:
    """每条建议必须引用本次运行里真实存在的工具结果，且数值对得上。"""
    ev = sug.get("evidence") or []
    if not ev:
        return False
    async with sm() as s:
        for e in ev:
            row = (await s.execute(select(ToolCallLog).where(
                ToolCallLog.run_id == run_id, ToolCallLog.call_id == e.get("call_id"),
                ToolCallLog.ok == 1))).scalar_one_or_none()
            if row is None or e.get("field") not in ("spend", "clicks", "orders"):
                return False
            item = next((k for k in row.result.get("keywords", [])
                         if k["keyword_id"] == e.get("keyword_id")), None)
            try:
                if item is None or abs(float(item[e["field"]]) - float(e["value"])) > 0.005:
                    return False
            except (KeyError, TypeError, ValueError):
                return False
    return any(e.get("keyword_id") == sug.get("target") for e in ev)
