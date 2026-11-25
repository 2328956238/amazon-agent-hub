"""专业 Agent 协议与注册表。

关键设计：`implemented` 是显式字段，未实现的 Agent 也必须能被注册和路由到，
但调度时必须原样返回「未实现」，**不允许用空壳冒充业务能力**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .state import Intent, AgentState, RunStatus


@dataclass(frozen=True)
class AgentResult:
    """专业 Agent 的统一返回结构。"""

    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    pending_approvals: tuple[dict[str, Any], ...] = ()


@runtime_checkable
class SpecialistAgent(Protocol):
    """专业 Agent 协议。实现方只负责自己的业务域，不做编排、不做审批。"""

    name: str
    intents: tuple[Intent, ...]
    implemented: bool

    async def run(self, state: AgentState) -> AgentResult: ...


class UnimplementedAgent:
    """占位 Agent：注册位置保留，调用时明确返回未实现。

    为什么不用 `raise NotImplementedError`：图里抛异常会变成 FAILED，
    和"用户说了一句暂时不支持的话"混在一起，语义不如 NOT_IMPLEMENTED 清楚。
    """

    def __init__(self, name: str, intents: tuple[Intent, ...], hint: str = ""):
        self.name = name
        self.intents = intents
        self.implemented = False
        self.hint = hint

    async def run(self, state: AgentState) -> AgentResult:
        return AgentResult(
            status=RunStatus.NOT_IMPLEMENTED.value,
            payload={
                "agent": self.name,
                "message": f"{self.name} Agent 尚未实现，本次没有执行任何业务操作。",
                "hint": self.hint or "该能力在后续阶段接入（商品 / 库存 / 定价）",
            },
        )


class AgentRegistry:
    """意图 → Agent 的注册表。一个 Agent 可以声明多个意图，但一个意图只能有一个 Agent。"""

    def __init__(self) -> None:
        self._by_name: dict[str, SpecialistAgent] = {}
        self._by_intent: dict[Intent, SpecialistAgent] = {}

    def register(self, agent: SpecialistAgent) -> None:
        if agent.name in self._by_name:
            raise ValueError(f"agent already registered: {agent.name}")
        for intent in agent.intents:
            if intent in self._by_intent:
                raise ValueError(f"intent already claimed: {intent.value} by {self._by_intent[intent].name}")
            self._by_intent[intent] = agent
        self._by_name[agent.name] = agent

    def get(self, name: str) -> SpecialistAgent | None:
        return self._by_name.get(name)

    def get_by_intent(self, intent: Intent) -> SpecialistAgent | None:
        return self._by_intent.get(intent)

    def names(self) -> list[str]:
        return list(self._by_name)

    def describe(self) -> list[dict[str, Any]]:
        """给 API 用的能力清单，未实现的如实标注。"""
        out = []
        for agent in self._by_name.values():
            out.append({
                "name": agent.name,
                "intents": [i.value for i in agent.intents],
                "implemented": agent.implemented,
            })
        return out


def default_registry(advertising: SpecialistAgent,
                     ops_agents: list[SpecialistAgent] | None = None) -> AgentRegistry:
    """注册能力。

    - advertising：广告诊断（写操作走审批链），已实现；
    - ops_agents：只读运营 Agent（product / inventory / orders / ad_performance / overview）；
    - 不传 ops_agents 时，product / inventory 保持"未实现"占位（兼容旧调用与旧测试）；
    - pricing 始终是占位：定价写操作需要真实平台与审批，本阶段不实现。
    """
    registry = AgentRegistry()
    registry.register(advertising)

    if ops_agents:
        for agent in ops_agents:
            registry.register(agent)
    else:
        registry.register(UnimplementedAgent(
            "product", (Intent.PRODUCT,), "只读运营 Agent 未注入（需 LocalDataAdapter）"))
        registry.register(UnimplementedAgent(
            "inventory", (Intent.INVENTORY,), "只读运营 Agent 未注入（需 LocalDataAdapter）"))

    registry.register(UnimplementedAgent(
        "pricing", (Intent.PRICING,), "下一阶段：定价策略、调价建议与审批（写操作）"))
    return registry
