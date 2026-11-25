"""编排层共享状态与枚举。

状态必须全部是 JSON 可序列化的原始类型——LangGraph 的 SQLite checkpointer
会把 state 存进 checkpoint 表，塞进 ORM 对象或 dataclass 会存不进去。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, TypedDict


class Intent(str, Enum):
    """用户意图。新增业务时在这里加，同时在 registry 注册对应 Agent。"""

    ADVERTISING = "advertising"          # 广告诊断 / 调价建议（写操作需审批）
    AD_PERFORMANCE = "ad_performance"    # 广告表现分析（只读，用 domain 指标）
    ORDER = "order"                      # 订单查询（只读）
    PRODUCT = "product"                  # 商品与 Listing 查询（只读）
    INVENTORY = "inventory"              # 库存查询与缺货风险（只读）
    PRICING = "pricing"                  # 智能定价（写操作，本阶段未实现）
    OVERVIEW = "overview"                # 综合运营分析（只读，多数据源汇总）
    UNKNOWN = "unknown"                  # 无法识别，交给 fail 节点


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    AWAITING_APPROVAL = "awaiting_approval"
    NOT_IMPLEMENTED = "not_implemented"
    FAILED = "failed"


class AgentState(TypedDict, total=False):
    """一次编排运行的完整状态。

    字段说明：
    - run_id / thread_id：run_id 是本次运行 id，thread_id 是 LangGraph 的会话 id（恢复靠它）
    - shop_id：**由后端认证上下文注入**，绝不从请求体或模型输出里取
    - intent / intent_source：意图及识别来源（rule / llm / fallback）
    - selected_agent / agent_implemented：路由结果；未实现的 Agent 也要如实标记
    - status：RunStatus 的值
    - result：最终汇总（API 直接返回它）
    - pending_approvals / approval_results：人工审批中断与恢复的载荷
    - errors：可读错误列表，不吞异常
    """

    run_id: str
    thread_id: str
    shop_id: str
    message: str
    intent: str
    intent_source: str
    selected_agent: str
    agent_implemented: bool
    status: str
    steps: int
    result: dict[str, Any]
    pending_approvals: list[dict[str, Any]]
    approval_results: list[dict[str, Any]]
    errors: list[str]


def initial_state(*, run_id: str, thread_id: str, shop_id: str, message: str) -> AgentState:
    return AgentState(
        run_id=run_id,
        thread_id=thread_id,
        shop_id=shop_id,
        message=message,
        intent=Intent.UNKNOWN.value,
        intent_source="",
        selected_agent="",
        agent_implemented=False,
        status=RunStatus.RUNNING.value,
        steps=0,
        result={},
        pending_approvals=[],
        approval_results=[],
        errors=[],
    )
