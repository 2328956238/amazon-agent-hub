"""Agent HTTP 接口：统一入口 + 运行查询 + 审批恢复。

安全边界（本文件最重要的部分）：
1. **shop_id 只来自后端认证上下文**：请求体里不允许出现 shop_id（Pydantic extra="forbid"），
   客户端无法指定要操作哪个店；
2. 无 token → 401；token 不在映射表里 → 401（不做"没配置就放行"）；
3. LangGraph 未安装/初始化失败 → 503，且**不影响** /webhook/amazon；
4. 写操作仍然只能由 approval.py 执行，API 不提供任何"直接改价"入口。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..adapters.local_ads import LocalAdsAdapter
from ..agent.llm import FakeLLM, LLMResponse
from ..agent.platform import InMemoryPlatform
from ..db import SessionLocal
from ..orchestration.agents.advertising import AdvertisingAgent, AdvertisingDeps
from ..orchestration.agents.ops import (
    AdPerformanceAgent,
    InventoryAgent,
    OpsDeps,
    OrdersAgent,
    OverviewAgent,
    ProductAgent,
)
from ..orchestration.graph import build_graph
from ..orchestration.nodes import OrchestratorDeps
from ..orchestration.registry import default_registry
from ..orchestration.state import RunStatus, initial_state

logger = logging.getLogger("hub.api.agent")

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------
class ApprovalItem(BaseModel):
    """单条审批决定。

    安全要点：**不接受客户端传入 actor / role**。身份与角色只能来自认证上下文
    （token 映射），否则 operator 只要在请求体里写 role="admin" 就能越权批准高风险操作。
    extra="forbid" 会让传了 actor/role 的请求直接 422。
    """

    model_config = ConfigDict(extra="forbid")

    suggestion_id: str = Field(min_length=1, max_length=64)
    decision: Literal["approve", "reject"]


class ChatRequest(BaseModel):
    """注意：没有 shop_id 字段，且 extra="forbid" —— 客户端传 shop_id 会直接 422。"""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = Field(default=None, max_length=64)
    approval: list[ApprovalItem] | None = None


# ---------------------------------------------------------------------------
# 认证上下文：店从哪里来
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AuthContext:
    shop_id: str
    actor: str
    role: str
    token_id: str


def _token_map() -> dict[str, dict[str, str]]:
    """token → 身份映射。默认只给一个开发 token；生产用 API_TOKENS 环境变量覆盖。"""
    raw = os.getenv("API_TOKENS", "")
    if raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=500, detail={
                "code": "bad_token_config", "message": f"API_TOKENS 不是合法 JSON: {exc}"})
        if not isinstance(data, dict):
            raise HTTPException(status_code=500, detail={
                "code": "bad_token_config", "message": "API_TOKENS 必须是 {token: {shop_id,...}} 结构"})
        return data

    # fail-closed：生产环境不允许使用内置开发 token（否则 dev-token 就是万能管理员）
    if os.getenv("APP_ENV", "dev").strip().lower() == "production":
        raise HTTPException(status_code=500, detail={
            "code": "auth_not_configured",
            "message": "APP_ENV=production 时必须配置 API_TOKENS，禁止使用内置开发 token",
        })

    dev_token = os.getenv("DEFAULT_API_TOKEN", "dev-token")
    return {dev_token: {
        "shop_id": os.getenv("DEFAULT_SHOP_ID", "S1"),
        "actor": os.getenv("DEFAULT_ACTOR", "dev"),
        "role": os.getenv("DEFAULT_ROLE", "admin"),
    }}


async def get_auth_context(authorization: str | None = Header(default=None)) -> AuthContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail={
            "code": "unauthorized", "message": "缺少 Bearer token"})
    token = authorization.split(" ", 1)[1].strip()
    entry = _token_map().get(token)
    if not entry or not entry.get("shop_id"):
        raise HTTPException(status_code=401, detail={
            "code": "unauthorized", "message": "token 无效"})
    return AuthContext(
        shop_id=entry["shop_id"],
        actor=entry.get("actor", "unknown"),
        role=entry.get("role", "operator"),
        token_id=token[:6] + "***",
    )


# ---------------------------------------------------------------------------
# 编排器（由 main.py 的 lifespan 初始化）
# ---------------------------------------------------------------------------
class Orchestrator:
    """薄封装：把图调用、状态读取、恢复动作收在一处。不含业务逻辑。"""

    def __init__(self, graph, deps: OrchestratorDeps):
        self.graph = graph
        self.deps = deps

    @staticmethod
    def _cfg(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    async def _snapshot(self, thread_id: str) -> tuple[dict[str, Any], tuple[str, ...]]:
        snap = await self.graph.aget_state(self._cfg(thread_id))
        return dict(snap.values or {}), tuple(snap.next or ())

    async def start(self, *, shop_id: str, message: str, thread_id: str | None) -> dict[str, Any]:
        tid = thread_id or str(uuid.uuid4())
        state = initial_state(run_id=str(uuid.uuid4()), thread_id=tid,
                              shop_id=shop_id, message=message)
        await self.graph.ainvoke(state, self._cfg(tid))
        return await self.view(tid)

    async def ensure_thread_owner_or_new(self, thread_id: str, shop_id: str) -> None:
        """启动前校验：thread 已存在且属于别的店 → 404（不泄露存在性）；不存在则允许新建。"""
        values, _ = await self._snapshot(thread_id)
        if not values:
            return
        owner = values.get("shop_id")
        if owner and owner != shop_id:
            raise HTTPException(status_code=404, detail={
                "code": "run_not_found", "message": "运行记录不存在"})

    async def ensure_can_resume(self, thread_id: str, shop_id: str) -> None:
        """恢复审批前校验：thread 必须存在且属于当前店，否则 404。"""
        values, _ = await self._snapshot(thread_id)
        if not values:
            raise HTTPException(status_code=404, detail={
                "code": "run_not_found", "message": "运行记录不存在"})
        if values.get("shop_id") != shop_id:
            raise HTTPException(status_code=404, detail={
                "code": "run_not_found", "message": "运行记录不存在"})

    async def resume(self, *, thread_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        from langgraph.types import Command  # 延迟导入：未装 langgraph 时不影响模块导入

        await self.graph.ainvoke(Command(resume={"items": items}), self._cfg(thread_id))
        return await self.view(thread_id)

    async def view(self, thread_id: str) -> dict[str, Any]:
        values, next_nodes = await self._snapshot(thread_id)
        if not values:
            raise HTTPException(status_code=404, detail={
                "code": "run_not_found", "message": f"没有 thread_id={thread_id} 的运行记录"})

        summary = values.get("result") or {}
        status = summary.get("status") or values.get("status") or RunStatus.RUNNING.value
        if next_nodes and status != RunStatus.FAILED.value:
            status = RunStatus.AWAITING_APPROVAL.value

        return {
            "run_id": summary.get("run_id") or values.get("run_id", ""),
            "thread_id": thread_id,
            "shop_id": values.get("shop_id", ""),
            "status": status,
            "intent": values.get("intent", ""),
            "intent_source": values.get("intent_source", ""),
            "agent": values.get("selected_agent", ""),
            "agent_implemented": bool(values.get("agent_implemented", False)),
            "model_mode": self.deps.model_mode,
            "payload": summary.get("payload") or values.get("result") or {},
            "pending_approvals": values.get("pending_approvals", []),
            "approval_results": values.get("approval_results", []),
            "errors": values.get("errors", []),
            "steps": values.get("steps", 0),
            "next_nodes": list(next_nodes),
        }


def default_llm_factory():
    """默认模型：FakeLLM（立刻给出"无建议"的结论）。

    诚实标注：API 响应里 model_mode="fake"。接真实模型只需换这个工厂，
    图、工具、审批链一行都不用改。
    """
    return FakeLLM([LLMResponse(kind="final", tokens=0, final={"suggestions": []})])


def build_orchestrator(checkpointer, *, sessionmaker=None, ads=None,
                       llm_factory=None, model_mode: str | None = None,
                       data=None) -> Orchestrator:
    """组装默认编排器：广告 Agent（写操作走审批）+ 只读运营 Agent + pricing 占位。"""
    sessionmaker = sessionmaker or SessionLocal
    if ads is None:
        seed = json.loads(os.getenv("ADS_SEED_BIDS", '{"S1": {"kw1": 1.0, "kw2": 0.8}}'))
        bids = {(shop, kw): float(v) for shop, kws in seed.items() for kw, v in kws.items()}
        ads = LocalAdsAdapter(InMemoryPlatform(bids))

    # 只读运营 Agent 依赖本地合成数据；没注入就退回未实现占位（不伪造能力）
    ops_agents = None
    if data is not None:
        ops_deps = OpsDeps(data=data)
        ops_agents = [ProductAgent(ops_deps), InventoryAgent(ops_deps),
                      OrdersAgent(ops_deps), AdPerformanceAgent(ops_deps),
                      OverviewAgent(ops_deps)]

    deps = OrchestratorDeps(
        registry=default_registry(AdvertisingAgent(AdvertisingDeps(
            sessionmaker=sessionmaker,
            ads=ads,
            llm_factory=llm_factory or default_llm_factory,
            today=lambda: date.today().isoformat(),
        )), ops_agents=ops_agents),
        sessionmaker=sessionmaker,
        ads=ads,
        intent_llm=None,
        model_mode=model_mode or os.getenv("LLM_MODE", "fake"),
    )
    return Orchestrator(graph=build_graph(deps, checkpointer=checkpointer), deps=deps)


async def get_orchestrator(request: Request) -> Orchestrator:
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        # 详细原因只写服务端日志；响应里只给通用文案，避免泄露路径/依赖/内部堆栈
        logger.error("Agent 接口不可用（编排层未初始化）: %s",
                     getattr(request.app.state, "orchestrator_error", "unknown"))
        raise HTTPException(status_code=503, detail={
            "code": "orchestrator_unavailable",
            "message": "Agent 服务暂不可用，请稍后重试或联系管理员；/webhook/amazon 不受影响",
        })
    return orch


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.post("/chat")
async def chat(payload: ChatRequest,
               ctx: AuthContext = Depends(get_auth_context),
               orch: Orchestrator = Depends(get_orchestrator)):
    """统一入口：一句话 → 意图识别 → 专业 Agent → 汇总；需要审批时返回 awaiting_approval。"""
    try:
        if payload.approval:
            if not payload.thread_id:
                raise HTTPException(status_code=422, detail={
                    "code": "thread_id_required", "message": "恢复审批必须带 thread_id"})
            # 跨店保护：先确认这个 thread 属于当前认证店铺，再允许恢复
            await orch.ensure_can_resume(payload.thread_id, ctx.shop_id)
            items = [
                # actor / role 一律取认证上下文，绝不采信请求体
                {**item.model_dump(), "actor": ctx.actor, "role": ctx.role}
                for item in payload.approval
            ]
            data = await orch.resume(thread_id=payload.thread_id, items=items)
        else:
            if payload.thread_id:
                # 复用 thread 前先校验归属，防止往别人的运行里注入状态
                await orch.ensure_thread_owner_or_new(payload.thread_id, ctx.shop_id)
            data = await orch.start(shop_id=ctx.shop_id, message=payload.message,
                                    thread_id=payload.thread_id)
    except HTTPException:
        raise
    except Exception:  # 图内部异常：500 + 通用文案（详细堆栈只进服务端日志）
        logger.exception("agent run failed (shop=%s thread=%s)", ctx.shop_id, payload.thread_id)
        raise HTTPException(status_code=500, detail={
            "code": "agent_run_failed",
            "message": "Agent 运行失败，请稍后重试；如持续失败请联系管理员",
        }) from None

    if data.get("shop_id") and data["shop_id"] != ctx.shop_id:
        # 理论上不可能（shop_id 由后端注入），一旦发生就是安全问题：直接拒绝返回
        raise HTTPException(status_code=403, detail={
            "code": "shop_mismatch", "message": "运行结果的店铺与认证店铺不一致，已拒绝返回"})
    return {"ok": True, "data": data}


@router.get("/runs/{thread_id}")
async def get_run(thread_id: str,
                  ctx: AuthContext = Depends(get_auth_context),
                  orch: Orchestrator = Depends(get_orchestrator)):
    data = await orch.view(thread_id)
    if data.get("shop_id") and data["shop_id"] != ctx.shop_id:
        raise HTTPException(status_code=404, detail={
            "code": "run_not_found", "message": "运行记录不存在"})  # 不泄露别的店的存在
    return {"ok": True, "data": data}


@router.get("/capabilities")
async def capabilities(_: AuthContext = Depends(get_auth_context),
                       orch: Orchestrator = Depends(get_orchestrator)):
    """能力清单：明确区分已实现 / 未实现，前端和面试演示都用它。"""
    return {"ok": True, "data": {
        "agents": orch.deps.registry.describe(),
        "model_mode": orch.deps.model_mode,
        "ads_mode": getattr(orch.deps.ads, "mode", None).value if getattr(orch.deps.ads, "mode", None) else "unknown",
    }}
