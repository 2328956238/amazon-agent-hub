"""Advertising Agent：把现有的「诊断 → 证据校验 → 提交建议」包装成专业 Agent。

设计原则：**零业务改动**。
- 复用 hub/agent/pipeline.py::diagnose_and_submit，一行逻辑都不改；
- 复用 hub/agent/approval.py 的规则/风险/审批，写操作仍然只能走 execute()；
- 本包装只做三件事：注入 ToolContext（shop_id 由后端给）、把结果转成可序列化结构、
  把「有待审批建议」翻译成编排层的 AWAITING_APPROVAL 状态。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from ...agent.pipeline import diagnose_and_submit
from ...agent.tools import ToolContext
from ..registry import AgentResult
from ..state import AgentState, Intent, RunStatus


@dataclass
class AdvertisingDeps:
    """广告 Agent 的依赖：全部通过构造注入，方便测试替换。"""

    sessionmaker: Any                 # async_sessionmaker
    ads: Any                          # AdsAdapter（get_bid / set_bid）
    llm_factory: Callable[[], Any]    # 每次运行要新的模型会话，不能复用 FakeLLM 的脚本状态
    today: Callable[[], str]          # 返回 YYYY-MM-DD，测试可注入固定日期


class AdvertisingAgent:
    name = "advertising"
    intents = (Intent.ADVERTISING,)
    implemented = True

    def __init__(self, deps: AdvertisingDeps):
        self.deps = deps

    async def run(self, state: AgentState) -> AgentResult:
        ctx = ToolContext(shop_id=state["shop_id"], today=self.deps.today())
        out = await diagnose_and_submit(
            self.deps.sessionmaker, self.deps.ads, self.deps.llm_factory(),
            ctx, state.get("message", ""),
        )

        run = out["run"]
        outcomes = [asdict(o) for o in out["outcomes"]]
        pending = [o for o in outcomes if o["status"] == "PENDING_APPROVAL"]

        payload: dict[str, Any] = {
            "agent": self.name,
            "diagnosis_run_id": run.run_id,
            "diagnosis_status": run.status,
            "steps": run.steps,
            "tokens": run.tokens,
            "dropped_no_evidence": out["dropped_no_evidence"],
            "suggestions": outcomes,
            "note": "建议不会自动修改平台；写操作必须经过审批链（approval.py）。",
        }
        status = RunStatus.AWAITING_APPROVAL.value if pending else RunStatus.COMPLETED.value
        return AgentResult(status=status, payload=payload, pending_approvals=tuple(pending))
