"""图组装：把节点和条件边连成 StateGraph。

图结构：

    START
      ↓
    understand ──(规则/LLM 判意图)
      ↓
    route ──┬── 没有 Agent 能处理 ──→ fail ─┐
            └── 有 Agent ──→ dispatch       │
                              │             │
              ┌───────────────┴─────────────┤
              │ 有待审批                    │
              ↓                             │
        approval_gate（interrupt 挂起）      │
              │                             │
              └──────────→ aggregate ←──────┘
                              ↓
                             END

LangGraph 只负责这条路径；业务与审批都留在各自的模块里。
"""

from __future__ import annotations

from functools import partial
from typing import Any

from .nodes import (
    OrchestratorDeps,
    after_dispatch,
    aggregate_node,
    approval_gate_node,
    dispatch_node,
    fail_node,
    route_condition,
    route_node,
    understand_node,
)
from .state import AgentState


class OrchestratorUnavailable(RuntimeError):
    """LangGraph 未安装或初始化失败。API 层据此返回 503，而不是 500。"""

    code = "orchestrator_unavailable"


def build_graph(deps: OrchestratorDeps, checkpointer: Any = None, *,
                allow_memory_fallback: bool = False):
    """编译 LangGraph 状态图。

    图中有人工审批闸门（interrupt），所以**必须有 checkpointer**。

    默认（allow_memory_fallback=False）不提供 checkpointer 会直接抛
    OrchestratorUnavailable —— 强制生产路径显式传入持久化 saver，
    避免"忘了配置就悄悄用内存、重启后审批记录全丢"。
    只有测试/单次演示可以显式 allow_memory_fallback=True 使用进程内存储。
    """
    try:
        from langgraph.graph import END, START, StateGraph
        from langgraph.checkpoint.memory import InMemorySaver
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径，用测试模拟
        raise OrchestratorUnavailable(
            "LangGraph 未安装：pip install langgraph", ) from exc

    if checkpointer is None:
        if not allow_memory_fallback:
            raise OrchestratorUnavailable(
                "build_graph 需要持久化 checkpointer（如 AsyncSqliteSaver）；"
                "测试请显式传 allow_memory_fallback=True")
        saver = InMemorySaver()
    else:
        saver = checkpointer

    graph = StateGraph(AgentState)
    graph.add_node("understand", partial(understand_node, deps=deps))
    graph.add_node("route", partial(route_node, deps=deps))
    graph.add_node("dispatch", partial(dispatch_node, deps=deps))
    graph.add_node("approval_gate", partial(approval_gate_node, deps=deps))
    graph.add_node("fail", partial(fail_node, deps=deps))
    graph.add_node("aggregate", partial(aggregate_node, deps=deps))

    graph.add_edge(START, "understand")
    graph.add_edge("understand", "route")
    graph.add_conditional_edges("route", route_condition,
                                {"dispatch": "dispatch", "fail": "fail"})
    graph.add_conditional_edges("dispatch", after_dispatch,
                                {"approval": "approval_gate", "aggregate": "aggregate"})
    graph.add_edge("approval_gate", "aggregate")
    graph.add_edge("fail", "aggregate")
    graph.add_edge("aggregate", END)

    return graph.compile(checkpointer=saver)
