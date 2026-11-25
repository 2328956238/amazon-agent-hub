"""编排节点：understand → route → dispatch → (approval_gate) → aggregate。

每个节点只返回 state 的局部更新；不写数据库业务、不直接改平台。
唯一会触发写操作的是 approval_gate_node，且它调用的是现有 approval.py 的
approve/reject/execute —— 审批链没有被绕过，也没有被重写。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from ..agent.approval import approve, execute, reject
from ..models import Suggestion
from .intent import IntentLLM, classify
from .registry import AgentRegistry
from .state import Intent, RunStatus


@dataclass
class OrchestratorDeps:
    """编排层依赖。全部注入，测试可替换成内存实现。"""

    registry: AgentRegistry
    sessionmaker: Any = None          # 审批恢复时需要读写 suggestion 表
    ads: Any = None                   # 审批通过后执行写操作的目标平台（AdsAdapter）
    intent_llm: IntentLLM | None = None
    model_mode: str = "fake"          # fake / real，用于 API 如实标注


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------
async def understand_node(state, deps: OrchestratorDeps) -> dict[str, Any]:
    intent, source = await classify(state.get("message", ""), deps.intent_llm)
    return {"intent": intent.value, "intent_source": source, "steps": state.get("steps", 0) + 1}


async def route_node(state, deps: OrchestratorDeps) -> dict[str, Any]:
    try:
        intent = Intent(state.get("intent", Intent.UNKNOWN.value))
    except ValueError:
        intent = Intent.UNKNOWN

    agent = deps.registry.get_by_intent(intent)
    if agent is None:
        return {
            "selected_agent": "",
            "agent_implemented": False,
            "status": RunStatus.FAILED.value,
            "errors": list(state.get("errors", [])) + [
                f"没有 Agent 能处理意图 {intent.value}，没有执行任何业务操作"],
        }
    return {"selected_agent": agent.name, "agent_implemented": bool(agent.implemented)}


def route_condition(state) -> str:
    """条件边：路由到 dispatch，或者直接进 fail。"""
    if state.get("status") == RunStatus.FAILED.value or not state.get("selected_agent"):
        return "fail"
    return "dispatch"


async def dispatch_node(state, deps: OrchestratorDeps) -> dict[str, Any]:
    agent = deps.registry.get(state.get("selected_agent", ""))
    if agent is None:
        return {"status": RunStatus.FAILED.value, "errors": list(state.get("errors", [])) + [
            f"注册表里找不到 Agent: {state.get('selected_agent')}"]}

    # 未实现的 Agent 也走正常调度，由它自己返回 NOT_IMPLEMENTED，不在这里假装成功
    try:
        result = await agent.run(state)
    except Exception as exc:  # 业务异常不吞掉，转成 FAILED 并保留错误文本
        return {"status": RunStatus.FAILED.value, "errors": list(state.get("errors", [])) + [
            f"{type(exc).__name__}: {exc}"]}

    return {
        "status": result.status,
        "result": result.payload,
        "pending_approvals": list(result.pending_approvals),
        "errors": list(state.get("errors", [])) + list(result.errors),
    }


def after_dispatch(state) -> str:
    """条件边：有待审批建议就进审批闸门，否则直接汇总。"""
    if state.get("status") == RunStatus.FAILED.value:
        return "aggregate"
    if state.get("pending_approvals"):
        return "approval"
    return "aggregate"


async def _suggestion_shop(deps: OrchestratorDeps, suggestion_id: str) -> str | None:
    """查建议属于哪个店铺。用于防止跨店审批（返回 None 表示不存在）。"""
    if deps.sessionmaker is None:
        return None
    async with deps.sessionmaker() as s:
        return (await s.execute(
            select(Suggestion.shop_id).where(Suggestion.id == suggestion_id))).scalar_one_or_none()


async def approval_gate_node(state, deps: OrchestratorDeps) -> dict[str, Any]:
    """人工审批闸门：interrupt 挂起，等外部带 decisions 恢复。

    恢复时调用现有 approval.approve / reject，通过后再调 execute。
    这里没有新写任何审批规则——规则仍在 approval.py 里。

    三道额外保护（都是编排层职责，不改 approval.py）：
    1. 建议必须属于当前运行的 shop_id，否则拒绝（防跨店审批）；
    2. 生产环境禁止写（require_non_production），当前阶段只允许 local/mock；
    3. 单条决策异常不影响其他条，错误写进 errors 而不是整图崩掉。
    """
    pending = state.get("pending_approvals") or []
    if not pending:
        return {}

    from langgraph.types import interrupt  # 延迟导入：不装 langgraph 时其余代码可导入

    decisions = interrupt({
        "type": "approval_required",
        "shop_id": state.get("shop_id"),
        "pending_approvals": pending,
        "how_to_resume": "用同一个 thread_id 再调用一次，并在 approval 字段里给出每条建议的决定",
    })

    shop_id = state.get("shop_id", "")
    items = (decisions or {}).get("items") or []
    applied: list[dict[str, Any]] = []
    errors = list(state.get("errors", []))

    for item in items:
        sid = item.get("suggestion_id", "")
        decision = (item.get("decision") or "reject").lower()
        actor = item.get("actor") or "unknown"
        role = item.get("role") or "operator"
        try:
            # 保护 1：跨店审批直接拒绝，不调用 approval.py
            owner = await _suggestion_shop(deps, sid)
            if owner != shop_id:
                applied.append({"suggestion_id": sid, "decision": decision,
                                "result": "forbidden_shop"})
                errors.append(f"建议 {sid} 不属于店铺 {shop_id}，已拒绝")
                continue

            if decision == "approve":
                r = await approve(deps.sessionmaker, sid, actor, role)
                if r == "approved":
                    # 保护 2：当前阶段不允许写生产环境
                    from ..adapters.base import require_non_production
                    require_non_production(deps.ads)
                    r = await execute(deps.sessionmaker, sid, deps.ads)
                    if r == "unknown":
                        # 执行结果未知：平台可能已经生效。明确要求人工核实，禁止自动补偿。
                        errors.append(
                            f"建议 {sid} 执行结果未知（平台可能已生效）：请先在平台侧核对当前值，"
                            f"再决定是否人工补偿；系统不会自动回滚")
            else:
                r = await reject(deps.sessionmaker, sid, actor)
        except Exception as exc:  # 保护 3：单条失败不影响其他条，也不吞错误
            r = f"error: {type(exc).__name__}: {exc}"
            errors.append(f"建议 {sid} 处理失败：{r}")
        applied.append({"suggestion_id": sid, "decision": decision, "result": r})

    handled = {a["suggestion_id"] for a in applied if not str(a["result"]).startswith("forbidden")}
    left = [p for p in pending if p.get("suggestion_id") not in handled]
    status = (RunStatus.AWAITING_APPROVAL.value if left
              else (RunStatus.FAILED.value if errors else RunStatus.COMPLETED.value))
    return {
        "approval_results": applied,
        "pending_approvals": left,
        "errors": errors,
        "status": status,
    }


async def fail_node(state, deps: OrchestratorDeps) -> dict[str, Any]:
    errors = list(state.get("errors", []))
    if not errors:
        errors.append("编排失败：无法路由到可执行的 Agent")
    return {"status": RunStatus.FAILED.value, "errors": errors}


async def aggregate_node(state, deps: OrchestratorDeps) -> dict[str, Any]:
    """统一结果汇总。API 直接返回这个结构，不再二次加工。"""
    summary = {
        "run_id": state.get("run_id", ""),
        "thread_id": state.get("thread_id", ""),
        "shop_id": state.get("shop_id", ""),
        "status": state.get("status", RunStatus.FAILED.value),
        "intent": state.get("intent", Intent.UNKNOWN.value),
        "intent_source": state.get("intent_source", ""),
        "agent": state.get("selected_agent", ""),
        "agent_implemented": bool(state.get("agent_implemented", False)),
        "model_mode": deps.model_mode,
        "payload": state.get("result", {}),
        "approval_results": state.get("approval_results", []),
        "errors": state.get("errors", []),
        "steps": state.get("steps", 0),
    }
    return {"result": summary, "steps": state.get("steps", 0) + 1}
