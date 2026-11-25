import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select, text

from ..models import ApprovalLog, Suggestion


def _is_unknown_outcome(exc: BaseException) -> bool:
    """异常是否属于「执行结果未知」。

    判断依据：请求已经发出去了，但没等到响应（超时/连接中断）。
    这种情况下平台侧可能已经生效，**不能当失败处理、不能自动回滚**。
    其他异常（本地校验失败、平台明确返回错误）属于「确定失败」，可以标记 FAILED。
    """
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    try:  # httpx 是可选依赖，没装也不影响判断
        import httpx
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))

DEFAULT_RULES = {
    "max_change_ratio": 0.30,               # 单次调幅上限
    "min_bid": 0.10,                        # 出价下限
    "max_changes_per_target_per_day": 2,    # 同一对象每天最多改几次
    "auto_approve_ratio": 0.10,             # 调幅不超过它视为低风险，可自动通过
    "high_ratio": 0.20,                     # 调幅超过它视为高风险，只有管理员能批
    "auto_approve_low": True,
}

# 哪种角色能批哪种风险
CAN_APPROVE = {"low": {"operator", "admin"}, "medium": {"operator", "admin"}, "high": {"admin"}}


@dataclass
class Outcome:
    suggestion_id: str
    status: str
    risk: str
    violations: list = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _transition(s, sid: str, frm: str, to: str) -> bool:
    """条件更新实现状态机：只有当前状态等于 frm 才会成功，并发下只有一个调用者赢。"""
    r = await s.execute(
        text("UPDATE suggestion SET status = :to, updated_at = :now WHERE id = :id AND status = :frm"),
        {"to": to, "now": _now(), "id": sid, "frm": frm})
    return r.rowcount == 1


async def _load(sm, sid: str):
    async with sm() as s:
        return (await s.execute(select(Suggestion).where(Suggestion.id == sid))).scalar_one_or_none()


async def _log(sm, sid, step, actor, result, detail=""):
    async with sm() as s:
        async with s.begin():
            s.add(ApprovalLog(suggestion_id=sid, step=step, actor=actor, result=result, detail=detail))


async def submit_suggestion(sm, platform, shop_id: str, run_id: int, today: str,
                            sug: dict, rules: dict = None) -> Outcome:
    rules = rules or DEFAULT_RULES
    sid = str(uuid.uuid4())
    before, after = float(sug["before"]), float(sug["after"])
    target = sug["target"]

    # 第一关：规则校验，全部由后端代码判断，不靠模型
    violations = []
    current = await platform.get_bid(shop_id, target)
    if abs(current - before) > 1e-9:
        violations.append("stale_before_value")       # 建议基于过期数据
    if after < rules["min_bid"]:
        violations.append("below_min_bid")
    ratio = round(abs(after - before) / before, 6) if before > 0 else 1.0
    if ratio > rules["max_change_ratio"]:
        violations.append("change_too_large")
    async with sm() as s:
        n_today = (await s.execute(text(
            "SELECT count(*) FROM suggestion WHERE shop_id = :s AND target = :t AND day = :d "
            "AND status IN ('APPROVED', 'EXECUTING', 'EXECUTED')"),
            {"s": shop_id, "t": target, "d": today})).scalar_one()
    if n_today >= rules["max_changes_per_target_per_day"]:
        violations.append("daily_limit_reached")

    # 第二关：风险分流
    if ratio <= rules["auto_approve_ratio"]:
        risk = "low"
    elif ratio <= rules["high_ratio"]:
        risk = "medium"
    else:
        risk = "high"

    if violations:
        status = "REJECTED_BY_RULE"
    elif risk == "low" and rules["auto_approve_low"]:
        status = "APPROVED"
    else:
        status = "PENDING_APPROVAL"

    async with sm() as s:
        async with s.begin():
            s.add(Suggestion(
                id=sid, shop_id=shop_id, run_id=run_id, day=today, action=sug["action"],
                target=target, before_value=before, after_value=after,
                reason=sug.get("reason", ""), evidence=sug.get("evidence", []),
                risk=risk, status=status, updated_at=_now()))
            s.add(ApprovalLog(suggestion_id=sid, step="rule_check", actor="system",
                              result="fail" if violations else "pass", detail=",".join(violations)))
            if status == "APPROVED":
                s.add(ApprovalLog(suggestion_id=sid, step="auto_approve", actor="system",
                                  result="ok", detail="low risk"))
    return Outcome(sid, status, risk, violations)


async def approve(sm, sid: str, actor: str, role: str) -> str:
    sug = await _load(sm, sid)
    if sug is None:
        return "not_found"
    if role not in CAN_APPROVE[sug.risk]:
        await _log(sm, sid, "approve", actor, "denied", f"role {role} cannot approve {sug.risk}")
        return "forbidden"
    async with sm() as s:
        async with s.begin():
            won = await _transition(s, sid, "PENDING_APPROVAL", "APPROVED")
            s.add(ApprovalLog(suggestion_id=sid, step="approve", actor=actor,
                              result="ok" if won else "conflict"))
    return "approved" if won else "conflict"


async def reject(sm, sid: str, actor: str) -> str:
    async with sm() as s:
        async with s.begin():
            won = await _transition(s, sid, "PENDING_APPROVAL", "REJECTED")
            s.add(ApprovalLog(suggestion_id=sid, step="reject", actor=actor,
                              result="ok" if won else "conflict"))
    return "rejected" if won else "conflict"


async def _finish(sm, sid, frm, to, step, detail=""):
    async with sm() as s:
        async with s.begin():
            await _transition(s, sid, frm, to)
            s.add(ApprovalLog(suggestion_id=sid, step=step, actor="system",
                              result="ok" if to in ("EXECUTED", "ROLLED_BACK") else "fail", detail=detail))


async def execute(sm, sid: str, platform) -> str:
    sug = await _load(sm, sid)
    if sug is None:
        return "not_found"
    # 抢占：APPROVED -> EXECUTING 只有一个并发调用者能成功，重复点击、重复调度都会被挡回
    async with sm() as s:
        async with s.begin():
            won = await _transition(s, sid, "APPROVED", "EXECUTING")
            s.add(ApprovalLog(suggestion_id=sid, step="execute_start", actor="system",
                              result="ok" if won else "conflict"))
    if not won:
        return "skipped"
    try:
        current = await platform.get_bid(sug.shop_id, sug.target)
        if abs(current - sug.before_value) > 1e-9:     # 执行前再核对一次，防审批期间数据已变
            raise RuntimeError("stale_before_value_at_execute")
        await platform.set_bid(sug.shop_id, sug.target, sug.after_value, idempotency_key=sid)
    except Exception as e:
        # 关键区分：超时类异常说明「请求可能已经到达平台，但没拿到结果」= 结果未知，
        # 绝不能当失败处理，更不能自动回滚（可能把已经生效的修改再改一次）。
        if _is_unknown_outcome(e):
            await _finish(sm, sid, "EXECUTING", "UNKNOWN", "execute_unknown", repr(e))
            return "unknown"
        await _finish(sm, sid, "EXECUTING", "FAILED", "execute_done", repr(e))
        return "failed"
    await _finish(sm, sid, "EXECUTING", "EXECUTED", "execute_done")
    return "executed"


async def rollback(sm, sid: str, platform) -> str:
    """回滚到 before_value。

    安全前置：回滚前必须确认平台当前值仍等于我们执行后的 after_value。
    如果期间被别人改过，回滚会覆盖别人的修改 —— 这种情况下拒绝回滚（conflict），
    交给人工判断，而不是"自动帮倒忙"。
    """
    sug = await _load(sm, sid)
    if sug is None:
        return "not_found"
    # 只有「已执行」状态才谈得上回滚：FAILED / UNKNOWN / 待审批 一律拒绝，
    # 避免"结果未知还去补偿"这种二次伤害。
    if sug.status != "EXECUTED":
        await _log(sm, sid, "rollback_precheck", "system", "skipped",
                   f"status={sug.status} is not rollback-able")
        return "skipped"
    try:
        current = await platform.get_bid(sug.shop_id, sug.target)
    except Exception as e:
        await _log(sm, sid, "rollback_precheck", "system", "fail",
                   f"cannot read current value: {e!r}")
        return "precheck_failed"
    if abs(current - sug.after_value) > 1e-9:
        await _log(sm, sid, "rollback_precheck", "system", "conflict",
                   f"current={current} != executed_after={sug.after_value}")
        return "conflict"

    async with sm() as s:
        async with s.begin():
            won = await _transition(s, sid, "EXECUTED", "ROLLING_BACK")
    if not won:
        return "skipped"
    try:
        await platform.set_bid(sug.shop_id, sug.target, sug.before_value,
                               idempotency_key=f"{sid}:rollback")
    except Exception as e:
        if _is_unknown_outcome(e):
            await _finish(sm, sid, "ROLLING_BACK", "ROLLBACK_UNKNOWN", "rollback_unknown", repr(e))
            return "unknown"
        await _finish(sm, sid, "ROLLING_BACK", "ROLLBACK_FAILED", "rollback", repr(e))
        return "rollback_failed"
    await _finish(sm, sid, "ROLLING_BACK", "ROLLED_BACK", "rollback")
    return "rolled_back"
