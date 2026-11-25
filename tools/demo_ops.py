"""五种业务场景演示：走真实 LangGraph 编排（不是直接调函数）。

运行（在项目根目录）：python tools/demo_ops.py

每个场景都会：
1. 用 LocalDataAdapter 加载对应场景覆盖（不改磁盘数据）
2. 通过 StateGraph 路由到只读 Agent
3. 打印 run 状态、命中的 Agent、关键指标

声明：数据全部是本地合成数据（synthetic），不是真实卖家数据。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 允许从项目根或 tools/ 两种方式运行
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub import config
from hub.adapters.local_data import LocalDataAdapter
from hub.adapters.local_ads import LocalAdsAdapter
from hub.agent.llm import FakeLLM, LLMResponse
from hub.agent.platform import InMemoryPlatform
from hub.orchestration.agents.advertising import AdvertisingAgent, AdvertisingDeps
from hub.orchestration.agents.ops import (
    AdPerformanceAgent,
    InventoryAgent,
    OpsDeps,
    OrdersAgent,
    OverviewAgent,
    ProductAgent,
)
from hub.orchestration.checkpointer import open_sqlite_checkpointer
from hub.orchestration.graph import build_graph
from hub.orchestration.nodes import OrchestratorDeps
from hub.orchestration.registry import default_registry
from hub.orchestration.state import initial_state


@dataclass
class Case:
    name: str
    scenario: str | None
    message: str
    show: str       # 打印 payload 的哪个部分


CASES = [
    Case("A 查询商品库存", None, "帮我查下 B0TEST0002 的库存", "inventory"),
    Case("B 查看最近订单", None, "最近订单有哪些", "orders"),
    Case("C 库存风险（正常）", "normal", "看看库存有没有缺货风险", "risk"),
    Case("C 库存风险（库存不足）", "low_inventory", "看看库存有没有缺货风险", "risk"),
    Case("C 库存风险（订单突增）", "order_spike", "看看库存有没有缺货风险", "risk"),
    Case("D 广告表现（正常）", "normal", "看下广告表现", "ad"),
    Case("D 广告表现（花费涨/销售降）", "advertising_drop", "看下广告表现", "ad"),
    Case("E 综合运营分析", "normal", "帮我做个综合运营分析", "overview"),
    Case("E 综合运营（价格异常）", "price_anomaly", "帮我做个综合运营分析", "overview"),
]


def build_deps(data: LocalDataAdapter) -> OrchestratorDeps:
    ads = LocalAdsAdapter(InMemoryPlatform({("S1", "kw1"): 1.0}))
    advertising = AdvertisingAgent(AdvertisingDeps(
        sessionmaker=None, ads=ads,
        llm_factory=lambda: FakeLLM([LLMResponse(kind="final", tokens=0, final={})]),
        today=lambda: "2026-01-10"))
    ops = OpsDeps(data=data)
    registry = default_registry(advertising, ops_agents=[
        ProductAgent(ops), InventoryAgent(ops), OrdersAgent(ops),
        AdPerformanceAgent(ops), OverviewAgent(ops)])
    return OrchestratorDeps(registry=registry, sessionmaker=None, ads=ads, model_mode="fake")


def summarize(payload: dict, show: str) -> str:
    if show == "inventory":
        rows = payload.get("items", [])
        return "; ".join(f"{r['sku']} 可售={r['available']}" for r in rows)
    if show == "orders":
        stats = payload.get("stats", {})
        return (f"{payload.get('count')} 条；7 天 {stats.get('order_count')} 单/"
                f"{stats.get('units')} 件/${stats.get('revenue')}；状态 {stats.get('by_status')}")
    if show == "risk":
        summary = payload.get("summary", {})
        bad = [i["sku"] + ":" + i["level"] for i in payload.get("items", [])
               if i["level"] != "ok"]
        return f"critical={summary.get('critical')} warn={summary.get('warn')} | " + \
               (", ".join(bad) if bad else "全部正常")
    if show == "ad":
        perf = payload.get("performance", {})
        m = perf.get("metrics", {})
        return (f"ACOS={m.get('acos'):.2%} ROAS={m.get('roas'):.2f} "
                f"CTR={m.get('ctr'):.2%} CVR={m.get('cvr'):.2%} "
                f"→ {perf.get('assessment', {}).get('level')}")
    if show == "overview":
        s = payload.get("summary", {})
        return (f"商品 {s.get('products')} / 7 天 {s.get('orders_7d')} 单 / "
                f"库存告急 {s.get('inventory_critical')} / 广告 {s.get('ad_level')} / "
                f"价格异常 {s.get('price_anomalies')} | 结论: {'; '.join(payload.get('highlights', []))}")
    return ""


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ops-demo-"))
    print(f"数据源: {config.AMAZON_DATA_DIR}  (source=synthetic)")
    print("=" * 100)
    async with open_sqlite_checkpointer(str(tmp / "cp.sqlite")) as saver:
        for i, case in enumerate(CASES, 1):
            data = LocalDataAdapter(config.AMAZON_DATA_DIR, scenario=case.scenario)
            deps = build_deps(data)
            graph = build_graph(deps, checkpointer=saver)
            thread = f"demo-{i}"
            state = initial_state(run_id=f"r{i}", thread_id=thread, shop_id="S1",
                                  message=case.message)
            out = await graph.ainvoke(state, {"configurable": {"thread_id": thread}})
            r = out["result"]
            print(f"\n[{case.name}]  场景={case.scenario or '默认'}")
            print(f"  意图={r['intent']}({r['intent_source']})  Agent={r['agent']}  "
                  f"状态={r['status']}  步数={r['steps']}")
            print(f"  -> {summarize(r['payload'], case.show)}")
    print("\n" + "=" * 100)
    print("说明：全部为本地合成数据；未连接任何真实 Amazon API；未执行任何写操作。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
